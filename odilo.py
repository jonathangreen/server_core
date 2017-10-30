# coding=utf-8
import base64
import datetime
import isbnlib
import json
import logging

from sqlalchemy.orm.session import Session

from httplib import HTTPException

from model import (
    get_one_or_create,
    Collection,
    Contributor,
    Credential,
    DataSource,
    DeliveryMechanism,
    Edition,
    ExternalIntegration,
    Hyperlink,
    Identifier,
    Representation,
    Subject,
)

from core.util.http import RequestNetworkException

from core.analytics import Analytics

from metadata_layer import (
    CirculationData,
    ContributorData,
    FormatData,
    IdentifierData,
    Metadata,
    LinkData,
    ReplacementPolicy,
    SubjectData,
)

from coverage import (
    BibliographicCoverageProvider,
)

from config import (
    CannotLoadConfiguration,
)

from testing import DatabaseTest

from util.http import (
    HTTP,
    BadResponseException,
)


class OdiloAPI(object):
    log = logging.getLogger("Odilo API")

    LIBRARY_API_BASE_URL = u"library_api_base_url"
    BASE_URL = "http://localhost:8080/api/v2"  # Debug value by default

    # --- OAuth ---
    TOKEN_ENDPOINT = BASE_URL + "/token"

    # --- Discovery API ---
    ALL_PRODUCTS_ENDPOINT = BASE_URL + "/records"

    RECORD_METADATA_ENDPOINT = BASE_URL + "/records/{recordId}"
    RECORD_AVAILABILITY_ENDPOINT = BASE_URL + "/records/{recordId}/availability"

    # --- Circulation API ---
    CHECKOUT_ENDPOINT = BASE_URL + "/records/{recordId}/checkout"
    CHECKOUT_GET = BASE_URL + "/checkouts/{checkoutId}"
    CHECKIN_ENDPOINT = BASE_URL + "/checkouts/{checkoutId}/return"
    # Downloads given checkout offering the url where to consume the digital resource.
    CHECKOUT_URL_ENDPOINT = BASE_URL + "/checkouts/{checkoutId}/download"

    PLACE_HOLD_ENDPOINT = BASE_URL + "/records/{recordId}/hold"
    HOLD_GET = BASE_URL + "/holds/{holdId}"
    RELEASE_HOLD_ENDPOINT = BASE_URL + "/holds/{holdId}/cancel"

    PATRON_CHECKOUTS_ENDPOINT = BASE_URL + "/patrons/{patronId}/checkouts"
    PATRON_HOLDS_ENDPOINT = BASE_URL + "/patrons/{patronId}/holds"

    def update_endpoints_url(self):
        # --- OAuth ---
        self.TOKEN_ENDPOINT = self.BASE_URL + "/token"

        # --- Discovery API ---
        self.ALL_PRODUCTS_ENDPOINT = self.BASE_URL + "/records"

        self.RECORD_METADATA_ENDPOINT = self.BASE_URL + "/records/{recordId}"
        self.RECORD_AVAILABILITY_ENDPOINT = self.BASE_URL + "/records/{recordId}/availability"

        # --- Circulation API ---
        self.CHECKOUT_ENDPOINT = self.BASE_URL + "/records/{recordId}/checkout"
        self.CHECKOUT_GET = self.BASE_URL + "/checkouts/{checkoutId}"
        self.CHECKIN_ENDPOINT = self.BASE_URL + "/checkouts/{checkoutId}/return"
        # Downloads given checkout offering the url where to consume the digital resource.
        self.CHECKOUT_URL_ENDPOINT = self.BASE_URL + "/checkouts/{checkoutId}/download"

        self.PLACE_HOLD_ENDPOINT = self.BASE_URL + "/records/{recordId}/hold"
        self.HOLD_GET = self.BASE_URL + "/holds/{holdId}"
        self.RELEASE_HOLD_ENDPOINT = self.BASE_URL + "/holds/{holdId}/cancel"

        self.PATRON_CHECKOUTS_ENDPOINT = self.BASE_URL + "/patrons/{patronId}/checkouts"
        self.PATRON_HOLDS_ENDPOINT = self.BASE_URL + "/patrons/{patronId}/holds"

    # ---------------------------------------

    PAGE_SIZE_LIMIT = 200

    def __init__(self, _db, collection):
        if collection.protocol != ExternalIntegration.ODILO:
            raise ValueError("Collection protocol is %s, but passed into OdiloAPI!" % collection.protocol)

        self._db = _db
        self.analytics = Analytics(self._db)

        self.collection_id = collection.id
        self.token = None
        self.client_key = collection.external_integration.username
        self.client_secret = collection.external_integration.password
        self.library_api_base_url = collection.external_integration.setting(self.LIBRARY_API_BASE_URL).value

        if not self.client_key or not self.client_secret or not self.library_api_base_url:
            raise CannotLoadConfiguration("Odilo configuration is incomplete.")

        # Use utf8 instead of unicode encoding
        settings = [self.client_key, self.client_secret, self.library_api_base_url]
        self.client_key, self.client_secret, self.library_api_base_url = (
            setting.encode('utf8') for setting in settings
        )

        # Set Odilo Connector API URL internally
        self.BASE_URL = self.library_api_base_url
        self.update_endpoints_url()

        # Get set up with up-to-date credentials from the API.
        self.check_creds()
        if not self.token:
            raise CannotLoadConfiguration("Invalid credentials for %s, cannot intialize API %s"
                                          % (self.client_key, self.library_api_base_url))

    @property
    def collection(self):
        return Collection.by_id(self._db, id=self.collection_id)

    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.ODILO)

    def check_creds(self, force_refresh=False):
        """If the Bearer Token has expired, update it."""
        if force_refresh:
            refresh_on_lookup = lambda x: x
        else:
            refresh_on_lookup = self.refresh_creds

        credential = self.credential_object(refresh_on_lookup)
        if force_refresh:
            self.refresh_creds(credential)
        self.token = credential.credential

    def credential_object(self, refresh):
        """Look up the Credential object that allows us to use
        the Odilo API.
        """
        return Credential.lookup(self._db, DataSource.ODILO, None, None, refresh)

    def refresh_creds(self, credential):
        """Fetch a new Bearer Token and update the given Credential object."""
        try:
            response = self.token_post(
                self.TOKEN_ENDPOINT,
                dict(grant_type="client_credentials"),
                allowed_response_codes=[200]
            )
            data = response.json()
        except (HTTPException, RequestNetworkException) as e:
            self.log.error("Cannot connect with %s, error: %s" % (self.TOKEN_ENDPOINT, e.message))
            return None

        if response.status_code == 200:
            self._update_credential(credential, data)
            self.token = credential.credential
            return 'OK'
        elif response.status_code == 400:
            response = response.json()
            message = response['error']
            if 'error_description' in response:
                message += '/' + response['error_description']
            return 'message'

    def get(self, url, extra_headers={}, exception_on_401=False):
        """Make an HTTP GET request using the active Bearer Token."""
        if extra_headers is None:
            extra_headers = {}
        headers = dict(Authorization="Bearer %s" % self.token)
        headers.update(extra_headers)
        status_code, headers, content = self._do_get(url, headers)
        if status_code == 401:
            if exception_on_401:
                # This is our second try. Give up.
                raise BadResponseException.from_response(
                    url,
                    "Something's wrong with the Odilo OAuth Bearer Token!",
                    (status_code, headers, content)
                )
            else:
                # Refresh the token and try again.
                self.check_creds(True)
                return self.get(url, extra_headers, True)
        else:
            return status_code, headers, content

    def token_post(self, url, payload, headers={}, **kwargs):
        """Make an HTTP POST request for purposes of getting an OAuth token."""
        s = "%s:%s" % (self.client_key, self.client_secret)
        auth = base64.standard_b64encode(s).strip()
        headers = dict(headers)
        headers['Authorization'] = "Basic %s" % auth
        headers['Content-Type'] = "application/x-www-form-urlencoded"
        return self._do_post(url, payload, headers, **kwargs)

    @staticmethod
    def _update_credential(credential, odilo_data):
        """Copy Odilo OAuth data into a Credential object."""
        credential.credential = odilo_data['token']
        expires_in = (odilo_data['expiresIn'] * 0.9)
        credential.expires = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)

    def get_metadata(self, record_id):
        identifier = record_id
        if isinstance(record_id, Identifier):
            identifier = record_id.identifier

        url = self.RECORD_METADATA_ENDPOINT.replace('{recordId}', identifier)

        response = self.get(url)

        if response and response.status == 200 and response.content:
            return response.content
        else:
            self.log.warn('Cannot retrieve metadata for record: ' + record_id + ' response http ' + response.status)
            if response.content:
                self.log.warn(response.content)
            return None

    def get_availability(self, record_id):
        url = self.RECORD_AVAILABILITY_ENDPOINT.replace('{recordId}', record_id)
        status_code, headers, content = self.get(url)
        content = json.loads(content)

        if status_code == 200 and len(content) > 0:
            return content
        else:
            self.log.warn('Cannot retrieve availability for record: ' + record_id + ' response http ' + status_code)
            if content:
                self.log.warn(content)
            return None

    @staticmethod
    def _do_get(url, headers, **kwargs):
        # More time please
        if not kwargs:
            kwargs = {"timeout": 60}
        else:
            kwargs['timeout'] = 60

        if 'allow_redirects' not in kwargs:
            kwargs['allow_redirects'] = True

        response = HTTP.get_with_timeout(url, headers=headers, **kwargs)
        return response.status_code, response.headers, response.content

    @staticmethod
    def _do_post(url, payload, headers, **kwargs):
        # More time please
        if not kwargs:
            kwargs = {"timeout": 60}
        else:
            kwargs['timeout'] = 60

        return HTTP.post_with_timeout(url, payload, headers=headers, **kwargs)


class MockOdiloAPI(OdiloAPI):
    @classmethod
    def mock_collection(cls, _db):
        library = DatabaseTest.make_default_library(_db)
        collection, ignore = get_one_or_create(
            _db, Collection,
            name="Test Odilo Collection",
            create_method_kwargs=dict(
                external_account_id=u'library_id_123',
            )
        )
        integration = collection.create_external_integration(
            protocol=ExternalIntegration.ODILO
        )
        integration.password = u'abcdef123hijklm'
        library.collections.append(collection)
        return collection


class OdiloRepresentationExtractor(object):
    """Extract useful information from Odilo's JSON representations."""

    log = logging.getLogger("OdiloRepresentationExtractor")

    format_data_for_odilo_format = {
        "ACSM": (
            Representation.PDF_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM
        ),
        "PDF": (
            Representation.PDF_MEDIA_TYPE, DeliveryMechanism.STREAMING_TEXT_CONTENT_TYPE
        ),
        "EPUB": (
            Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.STREAMING_TEXT_CONTENT_TYPE
        ),
        "MP3": (
            Representation.MP3_MEDIA_TYPE, DeliveryMechanism.STREAMING_AUDIO_CONTENT_TYPE
        ),
        "MP4": (
            Representation.MP4_MEDIA_TYPE, DeliveryMechanism.STREAMING_VIDEO_CONTENT_TYPE
        ),
        "WMV": (
            Representation.WMV_MEDIA_TYPE, DeliveryMechanism.STREAMING_VIDEO_CONTENT_TYPE
        ),
        "JPG": (
            Representation.JPEG_MEDIA_TYPE, DeliveryMechanism.NO_DRM
        ),
        "SCORM": (
            Representation.ZIP_MEDIA_TYPE, DeliveryMechanism.NO_DRM
        )
    }

    odilo_medium_to_simplified_medium = {
        "ACSM": Edition.BOOK_MEDIUM,
        "PDF": Edition.BOOK_MEDIUM,
        "EPUB": Edition.BOOK_MEDIUM,
        "MP3": Edition.AUDIO_MEDIUM,
        "MP4": Edition.VIDEO_MEDIUM,
        "WMV": Edition.VIDEO_MEDIUM,
        "JPG": Edition.IMAGE_MEDIUM,
        "SCORM": Edition.ELECTRONIC_FORMAT
    }

    @classmethod
    def record_info_to_circulation(cls, availability):
        """ Note:  The json data passed into this method is from a different file/stream 
        from the json data that goes into the record_info_to_metadata() method.
        """

        if 'recordId' not in availability:
            return None

        record_id = availability['recordId']
        primary_identifier = IdentifierData(Identifier.ODILO_ID, record_id)  # We own this availability.

        licenses_owned = int(availability['totalCopies'])
        licenses_available = int(availability['availableCopies'])
        if 'numLoans' in availability:
            licenses_checked_out = int(availability['numLoans'])
        licenses_reserved = int(availability['holdsQueueSize'])
        if 'numPatronsInHoldQueue' in availability:
            patrons_in_hold_queue = int(availability['numPatronsInHoldQueue'])
        else:
            patrons_in_hold_queue = 0

        return CirculationData(
            data_source=DataSource.ODILO,
            primary_identifier=primary_identifier,
            licenses_owned=licenses_owned,
            licenses_available=licenses_available,
            licenses_reserved=licenses_reserved,
            patrons_in_hold_queue=patrons_in_hold_queue,
        )

    @classmethod
    def image_link_to_linkdata(cls, link, rel):
        if not link:
            return None

        return LinkData(rel=rel, href=link, media_type=Representation.JPEG_MEDIA_TYPE)

    @classmethod
    def record_info_to_metadata(cls, book, availability):
        """Turn Odilo's JSON representation of a book into a Metadata
        object.

        Note:  The json data passed into this method is from a different file/stream 
        from the json data that goes into the book_info_to_circulation() method.
        """
        if 'id' not in book:
            return None

        odilo_id = book['id']
        primary_identifier = IdentifierData(Identifier.ODILO_ID, odilo_id)
        active = book.get('active')

        title = book.get('title')
        subtitle = book.get('subtitle')
        series = book.get('series')
        series_position = book.get('seriesPosition')

        contributors = []
        author = book.get('author')
        if author:
            roles = [Contributor.AUTHOR_ROLE]
            contributor = ContributorData(sort_name=author, display_name=author, roles=roles, biography=None)
            contributors.append(contributor)

        publisher = book.get('publisher')

        # Metadata --> Marc21 260$c
        published = book.get('publicationDate')
        if not published:
            # yyyyMMdd --> record creation date
            published = book.get('releaseDate')

        if published:
            try:
                published = datetime.datetime.strptime(published, "%Y%m%d")
            except ValueError as e:
                cls.log.warn('Cannot parse publication date from: ' + published + ', message: ' + e.message)

        # yyyyMMdd --> record last modification date
        last_update = book['modificationDate']
        if last_update:
            try:
                last_update = datetime.datetime.strptime(last_update, "%Y%m%d")
            except ValueError as e:
                cls.log.warn('Cannot parse last update date from: ' + last_update + ', message: ' + e.message)

        language = book.get('language')

        subjects = []
        for subject in book.get('subjects', []):
            subjects.append(SubjectData(type=Subject.TOPIC_TERM, identifier=subject, weight=100))

        grade_level = book.get('gradeLevel')
        if grade_level:
            subject = SubjectData(type=Subject.GRADE_LEVEL, identifier=grade_level, weight=10)
            subjects.append(subject)

        medium = None
        formats = []
        for format_received in book.get('formats', []):
            if format_received in cls.format_data_for_odilo_format:
                content_type, drm_scheme = cls.format_data_for_odilo_format.get(format_received)
                formats.append(FormatData(content_type, drm_scheme))
                if not medium:
                    medium = cls.odilo_medium_to_simplified_medium.get(format_received)
            else:
                cls.log.warn('Unrecognized format received: ' + format_received)

        if not medium:
            medium = Edition.BOOK_MEDIUM

        identifiers = []
        isbn = book.get('isbn')
        if isbn:
            if len(isbn) == 10:
                isbn = isbnlib.to_isbn13(isbn)
            identifiers.append(IdentifierData(Identifier.ISBN, isbn, 1))

        # A cover
        links = []
        cover_image_url = book.get('coverImageUrl')
        if cover_image_url:
            image_data = cls.image_link_to_linkdata(cover_image_url, Hyperlink.IMAGE)
            if image_data:
                links.append(image_data)

        # Descriptions become links.
        description = book.get('description')
        if description:
            links.append(LinkData(rel=Hyperlink.DESCRIPTION, content=description, media_type="text/html"))

        metadata = Metadata(
            data_source=DataSource.ODILO,
            title=title,
            subtitle=subtitle,
            language=language,
            medium=medium,
            series=series,
            series_position=series_position,
            publisher=publisher,
            published=published,
            primary_identifier=primary_identifier,
            identifiers=identifiers,
            subjects=subjects,
            contributors=contributors,
            links=links,
            data_source_last_updated=last_update
        )

        metadata.circulation = OdiloRepresentationExtractor.record_info_to_circulation(availability)
        metadata.circulation.formats = formats

        return metadata, active


class OdiloBibliographicCoverageProvider(BibliographicCoverageProvider):
    """Fill in bibliographic metadata for Odilo records.

    This will occasionally fill in some availability information for a
    single Collection, but we rely on Monitors to keep availability
    information up to date for all Collections.
    """

    SERVICE_NAME = "Odilo Bibliographic Coverage Provider"
    DATA_SOURCE_NAME = DataSource.ODILO
    PROTOCOL = ExternalIntegration.ODILO
    INPUT_IDENTIFIER_TYPES = Identifier.ODILO_ID

    def __init__(self, collection, api_class=OdiloAPI, **kwargs):
        """Constructor.

        :param collection: Provide bibliographic coverage to all
            Odilo books in the given Collection.
        :param api_class: Instantiate this class with the given Collection,
            rather than instantiating OdiloAPI.
        """
        super(OdiloBibliographicCoverageProvider, self).__init__(
            collection, **kwargs
        )
        if isinstance(api_class, OdiloAPI):
            # Use a previously instantiated OdiloAPI instance
            # rather than creating a new one.
            self.api = api_class
        else:
            # A web application should not use this option because it
            # will put a non-scoped session in the mix.
            _db = Session.object_session(collection)
            self.api = api_class(_db, collection)

        self.replacement_policy = ReplacementPolicy(
            identifiers=True,
            subjects=True,
            contributions=True,
            links=True,
            formats=True,
            rights=True,
            link_content=True,
            # even_if_not_apparently_updated=False,
            analytics=Analytics(self._db)
        )

    def process_item(self, record_id, record=None):
        if not record:
            record = self.api.get_metadata(record_id)

        if not record:
            return self.failure(record_id, 'Record not found', transient=False)

        # Retrieve availability
        availability = self.api.get_availability(record_id)

        metadata, is_active = OdiloRepresentationExtractor.record_info_to_metadata(record, availability)
        if not metadata:
            e = "Could not extract metadata from Odilo data: %s" % record_id
            return self.failure(record_id, e)

        identifier, made_new = metadata.primary_identifier.load(_db=self._db)

        if not identifier:
            e = "Could not create identifier for Odilo data: %s" % record_id
            return self.failure(identifier, e)

        identifier = self.set_metadata(identifier, metadata)

        # calls work.set_presentation_ready() for us
        if is_active:
            self.handle_success(identifier)

        return identifier, made_new
