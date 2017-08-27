"""
Transcript handlers.
"""
import json
import logging
import uuid

import boto
import django.dispatch
import requests
from boto.s3.key import Key
from django.db.models import Q
from pysrt import SubRipFile
from requests.packages.urllib3.exceptions import InsecurePlatformWarning
from rest_framework import status
from rest_framework.parsers import FormParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from control.veda_val import VALAPICall
from VEDA_OS01 import utils
from VEDA_OS01.models import (TranscriptCredentials, TranscriptProcessMetadata,
                              TranscriptProvider, TranscriptStatus,
                              VideoStatus)

requests.packages.urllib3.disable_warnings(InsecurePlatformWarning)

logging.basicConfig()
LOGGER = logging.getLogger(__name__)

# 3PlayMedia possible send-along statuses for a transcription callback.
COMPLETE = 'complete'
ERROR = 'error'

# Transcript format
TRANSCRIPT_SJSON = 'sjson'
CIELO24_TRANSCRIPT_COMPLETED = django.dispatch.Signal(providing_args=['job_id', 'lang_code', 'org', 'video_id'])
CIELO24_GET_CAPTION_URL = 'https://api.cielo24.com/api/job/get_caption'
CONFIG = utils.get_config()

# 3PlayMedia callback signal
THREE_PLAY_TRANSCRIPTION_DONE = django.dispatch.Signal(
    providing_args=['org', 'lang_code', 'edx_video_id', 'file_id', 'status', 'error_description']
)
# 3PlayMedia API URLs.
THREE_PLAY_TRANSCRIPT_URL = u'https://static.3playmedia.com/files/{file_id}/transcript.srt'
THREE_PLAY_TRANSLATION_SERVICES_URL = u'https://static.3playmedia.com/translation_services'
THREE_PLAY_ORDER_TRANSLATION_URL = u'https://api.3playmedia.com/files/{file_id}/translations/order'
THREE_PLAY_TRANSLATION_STATUS_URL = u'https://static.3playmedia.com/files/{file_id}/translations/{translation_id}'
THREE_PLAY_TRANSLATION_DOWNLOAD_URL = (u'https://static.3playmedia.com/files/{file_id}/translations/{translation_id}/'
                                       u'captions.srt')


class TranscriptError(Exception):
    """
    An error occurred during fetching transcript from cielo24.
    """
    pass


class TranscriptFetchError(TranscriptError):
    """
    An error occurred during fetching transcript from cielo24.
    """
    pass


class TranscriptTranslationError(TranscriptError):
    """
    An error occurred during the translation attempt on 3PlayMedia.
    """
    pass


class TranscriptConversionError(TranscriptError):
    """
    An error occurred during srt to sjson conversion.
    """
    pass


class TranscriptUploadError(TranscriptError):
    """
    An error occurred during sjson upload to s3.
    """
    pass


class AllowValidTranscriptProvider(AllowAny):
    """
    Permission class to allow only valid transcript provider.
    """
    def has_permission(self, request, view):
        """
        Check if request is from valid transcript provider.
        """
        try:
            return CONFIG['transcript_provider_request_token'] == view.kwargs['token']
        except KeyError:
            return False


class Cielo24CallbackHandlerView(APIView):
    """
    View to handler Cielo24 callback requests.
    """
    permission_classes = (AllowValidTranscriptProvider,)

    def get(self, request, **kwargs):
        """
        Handle Cielo24 callback request.
        """
        required_attrs = ('job_id', 'lang_code', 'org', 'video_id')
        missing = [attr for attr in required_attrs if attr not in request.query_params.keys()]
        if missing:
            LOGGER.warning(
                '[CIELO24 HANDLER] Required params are missing %s',
                missing,
            )
            return Response({}, status=status.HTTP_400_BAD_REQUEST)

        CIELO24_TRANSCRIPT_COMPLETED.send_robust(
            sender=self,
            org=request.query_params['org'],
            job_id=request.query_params['job_id'],
            video_id=request.query_params['video_id'],
            lang_code=request.query_params['lang_code'],
        )
        return Response()


@django.dispatch.receiver(CIELO24_TRANSCRIPT_COMPLETED, dispatch_uid="cielo24_transcript_completed")
def cielo24_transcript_callback(sender, **kwargs):
    """
    * download transcript(SRT) from Cielo24
    * convert SRT to SJSON
    * upload SJSON to AWS S3
    * update transcript status in VAL
    """
    process_metadata = None
    transcript_prefs = None

    org = kwargs['org']
    job_id = kwargs['job_id']
    video_id = kwargs['video_id']
    lang_code = kwargs['lang_code']

    LOGGER.info(
        '[CIELO24 TRANSCRIPTS] Transcript complete request received for video=%s -- org=%s -- lang=%s -- job_id=%s',
        video_id,
        org,
        lang_code,
        job_id
    )

    # get transcript credentials for an organization
    try:
        transcript_prefs = TranscriptCredentials.objects.get(
            org=org,
            provider=TranscriptProvider.CIELO24,
        )
    except TranscriptCredentials.DoesNotExist:
        LOGGER.exception('[CIELO24 TRANSCRIPTS] Unable to get transcript credentials for job_id=%s', job_id)

    # mark the transcript for a particular language as ready
    try:
        process_metadata = TranscriptProcessMetadata.objects.filter(
            provider=TranscriptProvider.CIELO24,
            process_id=job_id,
            lang_code=lang_code
        ).latest()
    except TranscriptProcessMetadata.DoesNotExist:
        LOGGER.exception(
            '[CIELO24 TRANSCRIPTS] Unable to get transcript process metadata for job_id=%s',
            job_id
        )

    # if transcript credentials are missing then we can do nothing
    if not transcript_prefs and process_metadata:
        process_metadata.status = TranscriptStatus.FAILED
        process_metadata.save()

    if transcript_prefs and process_metadata:
        api_key = transcript_prefs.api_key
        try:
            srt_data = fetch_srt_data(
                CIELO24_GET_CAPTION_URL,
                v=1,
                job_id=job_id,
                api_token=api_key,
                caption_format='SRT'
            )
        except TranscriptFetchError:
            process_metadata.status = TranscriptStatus.FAILED
            process_metadata.save()
            LOGGER.exception(
                '[CIELO24 TRANSCRIPTS] Fetch request failed for video=%s -- lang=%s -- job_id=%s',
                video_id,
                lang_code,
                job_id
            )
            return

        process_metadata.status = TranscriptStatus.READY
        process_metadata.save()

        try:
            sjson = convert_srt_to_sjson(srt_data)
            sjson_file_name = upload_sjson_to_s3(CONFIG, sjson)
        except Exception:
            LOGGER.exception(
                '[CIELO24 TRANSCRIPTS] Request failed for video=%s -- lang=%s -- job_id=%s.',
                video_id,
                lang_code,
                job_id
            )
            raise

        # update edx-val with completed transcript information
        val_api = VALAPICall(process_metadata.video, val_status=None)
        val_api.update_val_transcript(
            video_id=process_metadata.video.studio_id,
            lang_code=lang_code,
            name=sjson_file_name,
            transcript_format=TRANSCRIPT_SJSON,
            provider=TranscriptProvider.CIELO24
        )

        # update transcript status for video in edx-val only if all langauge transcripts are ready
        video_jobs = TranscriptProcessMetadata.objects.filter(video__studio_id=video_id)
        if all(video_job.status == TranscriptStatus.READY for video_job in video_jobs):
            val_api.update_video_status(process_metadata.video.studio_id, VideoStatus.TRANSCRIPT_READY)


def fetch_srt_data(url, **request_params):
    """
    Fetch srt data from transcript provider.
    """
    # return TRANSCRIPT_SRT_DATA
    response = requests.get(
        utils.build_url(url, **request_params)
    )

    if not response.ok:
        raise TranscriptFetchError(
            '[TRANSCRIPT FETCH ERROR] status={} -- text={}'.format(
                response.status_code,
                response.text
            )
        )

    return response.text


def convert_srt_to_sjson(srt_data):
    """
    Convert SRT to SJSON

    Arguments:
        srt_data: unicode, content of source subs.

    Returns:
        dict: SJSON data
    """
    srt_subs_obj = SubRipFile.from_string(srt_data)

    sub_starts = []
    sub_ends = []
    sub_texts = []

    for sub in srt_subs_obj:
        sub_starts.append(sub.start.ordinal)
        sub_ends.append(sub.end.ordinal)
        sub_texts.append(sub.text.replace('\n', ' '))

    subs = {
        'start': sub_starts,
        'end': sub_ends,
        'text': sub_texts
    }

    return subs


def upload_sjson_to_s3(config, sjson_data):
    """
    Upload sjson data to s3.
    """
    s3_conn = boto.connect_s3()
    bucket = s3_conn.get_bucket(config['aws_video_transcripts_bucket'])
    k = Key(bucket)
    k.content_type = 'application/json'
    k.key = '{directory}{uuid}.sjson'.format(
        directory=config['aws_video_transcripts_prefix'],
        uuid=uuid.uuid4().hex
    )
    k.set_contents_from_string(json.dumps(sjson_data))
    return k.key


class ThreePlayMediaCallbackHandlerView(APIView):
    """
    View to handle 3PlayMedia callback requests.
    """
    parser_classes = (FormParser,)
    permission_classes = (AllowValidTranscriptProvider,)

    def post(self, request, **kwargs):
        """
        Handle 3PlayMedia callback request.
        """
        required_attrs = ['file_id', 'status', 'org', 'edx_video_id']
        received_attributes = request.data.keys() + request.query_params.keys()
        missing = [attr for attr in required_attrs if attr not in received_attributes]
        if missing:
            LOGGER.warning(
                u'[3PlayMedia Callback] process_id=%s Received Attributes=%s Missing Attributes=%s',
                request.data.get('file_id'),
                received_attributes,
                missing,
            )
            return Response(status=status.HTTP_200_OK)

        # Dispatch 3playMedia transcription signal
        THREE_PLAY_TRANSCRIPTION_DONE.send_robust(
            sender=self,
            org=request.query_params['org'],
            edx_video_id=request.query_params['edx_video_id'],
            lang_code='en',
            file_id=request.data['file_id'],
            status=request.data['status'],
            # Following is going to be an error description if an error occurs during
            # 3playMedia transcription process
            error_description=request.data.get('error_description'),
        )
        return Response(status=status.HTTP_200_OK)


def get_translation_services(api_key):
    """
    GET available 3Play Media Translation services

    Arguments:
        api_key(unicode): api key which is required to make an authentic call to 3Play Media

    Returns:
        Available 3Play Media Translation services.
    """
    response = requests.get(utils.build_url(THREE_PLAY_TRANSLATION_SERVICES_URL, apikey=api_key))
    if not response.ok:
        raise TranscriptTranslationError(
            u'[3PlayMedia Callback] Error while fetching the translation services -- {status}, {response}'.format(
                status=response.status_code,
                response=response.text,
            )
        )

    # Response should be a list containing services, details:
    # http://support.3playmedia.com/hc/en-us/articles/227729988-Translations-API-Methods
    available_services = json.loads(response.text)
    if not isinstance(available_services, list):
        raise TranscriptTranslationError(
            u'[3PlayMedia Callback] Expected list but got: -- {response}.'.format(
                response=response.text,
            )
        )

    return available_services


def get_standard_translation_service(translation_services, target_language):
    """
    Get standard translation service
    Arguments:
         translation_services(list): List of available 3play media translation services.
         target_language(str): A language code whose standard translation service is needed.

    Returns:
        A translation service id or None.
    """
    translation_service_id = None
    for service in translation_services:
        service_found = (
            service['target_language_iso_639_1_code'] == target_language and
            service['service_level'] == 'standard'
        )
        if service_found:
            translation_service_id = service['id']
            break

    return translation_service_id


def place_translation_order(api_key, api_secret, translation_service_id, target_language, file_id):
    """
    Places a translation order on 3play media.

    Arguments:
        api_key(unicode): api key
        api_secret(unicode): api secret
        translation_service_id(unicode): translation service id got from 3Play Media
        target_language(unicode): A language code translation is being ordered
        file_id(unicode): 3play media file id / process id
    """
    order_response = requests.post(THREE_PLAY_ORDER_TRANSLATION_URL.format(file_id=file_id), json={
        'apikey': api_key,
        'api_secret_key': api_secret,
        'translation_service_id': translation_service_id,
    })
    if not order_response.ok:
        LOGGER.error(
            '[3PlayMedia Callback] An error occurred during translation, target language=%s, file_id=%s, status=%s',
            target_language,
            file_id,
            order_response.status_code,
        )
        return

    # Translation Order API returns `success` attribute specifying whether the order has been placed
    # successfully: http://support.3playmedia.com/hc/en-us/articles/227729988-Translations-API-Methods
    translation_order = json.loads(order_response.text)
    if not translation_order.get('success'):
        LOGGER.error(
            '[3PlayMedia Callback] Translation failed fot target language=%s, file_id=%s, response=%s',
            target_language,
            file_id,
            order_response.text,
        )
        return

    return translation_order


def order_translations(file_id, api_key, api_secret, target_languages):
    """
    Order translations on 3PlayMedia for all the target languages.

    Process:
        * Fetch all the pending translations process for a file
        * Fetch all the translation services from 3PlayMedia
        * For each process,
            - Find suitable translation service
            - Order translation from that service
            - Move the process to `in progress` and update it with the
              translation id received from 3Play.

    Arguments:
        file_id(unicode): File identifier
        api_key(unicode): API key
        api_secret(unicode): API Secret
        target_languages(list): List of language codes

    Raises:
        TranscriptTranslationError: when an error occurred while fetching the translation services.
    """
    if not target_languages:
        return

    translation_processes = TranscriptProcessMetadata.objects.filter(
        process_id=file_id,
        provider=TranscriptProvider.THREE_PLAY,
        status=TranscriptStatus.PENDING,
        lang_code__in=target_languages,
    )

    # Retrieve available translation services.
    try:
        available_services = get_translation_services(api_key)
    except TranscriptTranslationError:
        # Fail all the pending translation processes associated with this file id.
        translation_processes.update(status=TranscriptStatus.FAILED)
        raise

    for target_language in target_languages:
        # 1 - get a translation process for the target language
        try:
            translation_process = translation_processes.filter(lang_code=target_language).latest()
        except TranscriptProcessMetadata.DoesNotExist:
            LOGGER.warning(
                u'[3PlayMedia Callback] process not found for target language %s -- process id %s',
                target_language,
                file_id,
            )
            continue

        # 2 - Find a standard service for translation for the target language.
        translation_service_id = get_standard_translation_service(available_services, target_language)
        if translation_service_id is None:
            # Fail the process
            translation_process.update(status=TranscriptStatus.FAILED)
            LOGGER.error(
                u'[3PlayMedia Callback] No translation service found for target language %s -- process id %s',
                target_language,
                file_id,
            )
            continue

        # 3 - Place an order
        # At this point, we've got our service ready to use. Now, place an order for the translation.
        translation_order = place_translation_order(
            api_key=api_key,
            api_secret=api_secret,
            translation_service_id=translation_service_id,
            target_language=target_language,
            file_id=file_id,
        )
        if translation_order:
            translation_process.update(
                translation_id=translation_order['translation_id'],
                status=TranscriptStatus.IN_PROGRESS
            )
        else:
            translation_process.update(status=TranscriptStatus.FAILED)


def validate_transcript_response(edx_video_id, file_id, transcript, lang_code, log_prefix):
    """
    This validates transcript response received from 3Play Media.

     Arguments:
         edx_video_id(unicode): studio video identifier
         file_id(unicode): file identifier
         transcript(unicode): SRT transcript content ideally
         lang_code(unicode): language code
         log_prefix(unicode): A prefix for the emitted logs

    transcript is going to be SRT content and if this is not so, then it'll be a json response
    describing the error and process will be marked as failed. Error response will be logged
    along with the validation.
    """
    try:
        json.loads(transcript)
        # Log the details.
        LOGGER.error(
            u'[%s] Transcript fetch error for video=%s -- lang_code=%s -- process=%s -- response=%s',
            log_prefix,
            edx_video_id,
            lang_code,
            file_id,
            transcript,
        )
        return False
    except ValueError:
        pass

    return True


def get_transcript_credentials(provider, org, edx_video_id, file_id, log_prefix):
    """
    Get org-specific transcript credentials.

    Arguments:
        provider(TranscriptProvider): transcript provider
        org(unicode): organization extracted from course id
        log_prefix(unicode): A prefix for the emitted logs
        edx_video_id(unicode): studio video identifier
        file_id(unicode): file identifier or process identifier
    """
    transcript_secrets = None
    try:
        transcript_secrets = TranscriptCredentials.objects.get(org=org, provider=provider)
    except TranscriptCredentials.DoesNotExist:
        LOGGER.exception(
            u'[%s] Unable to get transcript secrets for org=%s, edx_video_id=%s, file_id=%s.',
            log_prefix,
            org,
            edx_video_id,
            file_id,
        )

    return transcript_secrets


@django.dispatch.receiver(THREE_PLAY_TRANSCRIPTION_DONE, dispatch_uid="three_play_transcription_done")
def three_play_transcription_callback(sender, **kwargs):
    """
    This is a receiver for 3Play Media callback signal.

    Arguments:
        sender: sender of the signal
        kwargs(dict): video transcription metadata

    Process:
        * download transcript(SRT) from 3PlayMedia
        * convert SRT to SJSON
        * upload SJSON to AWS S3
        * order translations for all the preferred languages
        * update transcript status in VAL
    """
    log_prefix = u'3PlayMedia Callback'
    # Extract all the must have attributes
    org = kwargs['org']
    edx_video_id = kwargs['edx_video_id']
    lang_code = kwargs['lang_code']
    file_id = kwargs['file_id']
    state = kwargs['status']

    try:
        process = TranscriptProcessMetadata.objects.filter(
            provider=TranscriptProvider.THREE_PLAY,
            process_id=file_id,
            lang_code=lang_code,
        ).latest()
    except TranscriptProcessMetadata.DoesNotExist:
        LOGGER.exception(
            u'[3PlayMedia Callback] Unable to get transcript process for org=%s, edx_video_id=%s, file_id=%s.',
            org,
            edx_video_id,
            file_id,
        )
        return

    # On completion of a transcript
    # Indicates that the default video speech transcription has been done successfully.
    if state == COMPLETE:
        log_args = (edx_video_id, lang_code, file_id)

        # 1 - Retrieve transcript credentials
        transcript_secrets = get_transcript_credentials(
            provider=TranscriptProvider.THREE_PLAY,
            org=org,
            edx_video_id=edx_video_id,
            file_id=file_id,
            log_prefix=log_prefix,
        )
        if not transcript_secrets:
            process.update(status=TranscriptStatus.FAILED)
            return

        # 2 - Fetch the transcript from 3Play Media.
        try:
            srt_transcript = fetch_srt_data(
                THREE_PLAY_TRANSCRIPT_URL.format(file_id=file_id),
                apikey=transcript_secrets.api_key,
            )
        except TranscriptFetchError:
            LOGGER.exception(
                u'[3PlayMedia Callback] Fetch request failed for video=%s -- lang_code=%s -- process_id=%s',
                *log_args
            )
            process.update(status=TranscriptStatus.FAILED)
            return

        # 3 - Validate transcript content received from 3Play Media and mark the transcription process.
        is_valid_transcript = validate_transcript_response(
            edx_video_id=edx_video_id,
            file_id=file_id,
            transcript=srt_transcript,
            lang_code=lang_code,
            log_prefix=log_prefix,
        )
        if is_valid_transcript:
            process.update(status=TranscriptStatus.READY)
        else:
            process.update(status=TranscriptStatus.FAILED)

        # 4 - Convert SRT transcript to SJson format and upload it to S3.
        try:
            sjson_transcript = convert_srt_to_sjson(srt_transcript)
            sjson_file = upload_sjson_to_s3(CONFIG, sjson_transcript)
        except Exception:
            # in case of any exception, log and raise.
            LOGGER.exception(
                u'[3PlayMedia Callback] Request failed for video=%s -- lang_code=%s -- process_id=%s',
                *log_args
            )
            raise

        # 5 - Update edx-val with completed transcript information.
        val_api = VALAPICall(video_proto=None, val_status=None)
        val_api.update_val_transcript(
            video_id=process.video.studio_id,
            lang_code=lang_code,
            name=sjson_file,
            transcript_format=TRANSCRIPT_SJSON,
            provider=TranscriptProvider.THREE_PLAY,
        )

        # 6 - Translation Phase
        # That's the phase for kicking off translation processes for all the
        # preferred languages except the video's speech language.
        target_languages = list(process.video.preferred_languages)
        target_languages.remove(lang_code)

        # Create the translation tracking processes for all the target languages.
        for target_language in target_languages:
            TranscriptProcessMetadata.objects.create(
                video=process.video,
                provider=TranscriptProvider.THREE_PLAY,
                process_id=file_id,
                lang_code=target_language,
                status=TranscriptStatus.PENDING,
            )

        # Order translations for target languages
        try:
            order_translations(file_id, transcript_secrets.api_key, transcript_secrets.api_secret, target_languages)
        except TranscriptTranslationError:
            LOGGER.exception(
                u'[3PlayMedia Callback] Translation could not be performed - video=%s, lang_code=%s, file_id=%s.',
                *log_args
            )
        except Exception:
            LOGGER.exception(
                u'[3PlayMedia Callback] Error while translating the transcripts - video=%s, lang_code=%s, file_id=%s',
                *log_args
            )
            raise

        # 7 - Update transcript status.
        # It will be for edx-val as well as edx-video-pipeline and this will be the case when
        # there is only one transcript language for a video(that is, already been processed).
        if not target_languages:
            val_api.update_video_status(process.video.studio_id, VideoStatus.TRANSCRIPT_READY)

        # On success, a happy farewell log.
        LOGGER.info(
            (u'[3PlayMedia Callback] Video speech transcription was successful for'
             u' video=%s -- lang_code=%s -- process_id=%s'),
            *log_args
        )

    elif state == ERROR:
        # Fail the process
        process.status = TranscriptStatus.FAILED
        process.save()
        # Log the error information
        LOGGER.error(
            u'[3PlayMedia Callback] Error while transcription - error=%s, org=%s, edx_video_id=%s, file_id=%s.',
            kwargs['error_description'],
            org,
            edx_video_id,
            file_id,
        )
    else:
        # Status must be either 'complete' or 'error'
        # more details on http://support.3playmedia.com/hc/en-us/articles/227729828-Files-API-Methods
        LOGGER.error(
            u'[3PlayMedia Callback] Got invalid status - status=%s, org=%s, edx_video_id=%s, file_id=%s.',
            state,
            org,
            edx_video_id,
            file_id,
        )


def get_translation_status(api_key, file_id, translation_id, edx_video_id, lang_code):
    """
    Get translation status for a translation process from 3Play Media.

    Arguments:
        api_key(unicode): api key
        file_id(unicode): file identifier or process identifier
        translation_id(unicode): translation identifier associated with that file identifier
        edx_video_id(unicode): video studio identifier
        lang_code(unicode): language code

    Returns:
        A translation status retrieved from 3play media or None in case of a faulty response.
    """
    translation_status_url = utils.build_url(
        THREE_PLAY_TRANSLATION_STATUS_URL.format(
            file_id=file_id,
            translation_id=translation_id,
        ),
        apikey=api_key
    )
    translation_status_response = requests.get(translation_status_url)
    if not translation_status_response.ok:
        LOGGER.error(
            (u'[3PlayMedia Task] Translation status request failed for video=%s -- '
             u'lang_code=%s -- process_id=%s -- status=%s'),
            edx_video_id,
            lang_code,
            file_id,
            translation_status_response.status_code,
        )
        return

    translation_status = json.loads(translation_status_response.text)
    if translation_status.get('iserror'):
        LOGGER.error(
            (u'[3PlayMedia Task] unable to get translation status for video=%s -- '
             u'lang_code=%s -- process_id=%s -- response=%s'),
            edx_video_id,
            lang_code,
            file_id,
            translation_status_response.text,
        )
        return

    return translation_status


def retrieve_three_play_translations():
    """
    Checks translation status on 3PlayMedia for all the progressing processes, fetches them if they're complete.

    Retrieval flow:
    1. Fetches 3PlayMedia translation processes whose status is `in progress`
    2. For each process, retrieve the org-wide api keys
    3. Check translation status through 3PlayMedia
    4. If its done, mark the process as complete, fetch translated transcript, convert to sjson, upload it to s3 and
    finally, update it in edx-val.
    """
    log_prefix = u'3PlayMedia Task'
    translation_processes = TranscriptProcessMetadata.objects.filter(
        provider=TranscriptProvider.THREE_PLAY,
        status=TranscriptStatus.IN_PROGRESS,
    ).exclude(Q(translation_id__isnull=True) | Q(translation_id__exact=''))

    for translation_process in translation_processes:

        log_args = (
            translation_process.video.studio_id,
            translation_process.lang_code,
            translation_process.process_id,
        )

        course_id = translation_process.video.inst_class.local_storedir.split(',')[0]
        org = utils.extract_course_org(course_id=course_id)

        # Retrieve transcript credentials
        three_play_secrets = get_transcript_credentials(
            provider=TranscriptProvider.THREE_PLAY,
            org=org,
            edx_video_id=translation_process.video.studio_id,
            file_id=translation_process.process_id,
            log_prefix=log_prefix
        )
        if not three_play_secrets:
            # Fail the process
            translation_process.update(status=TranscriptStatus.FAILED)
            continue

        # Check transcript status
        translation_status = get_translation_status(
            three_play_secrets.api_key,
            translation_process.process_id,
            translation_process.translation_id,
            translation_process.video.studio_id,
            translation_process.lang_code,
        )

        if not translation_status:
            # Fail the process
            translation_process.update(status=TranscriptStatus.FAILED)
            continue

        # On a complete translation
        if translation_status['state'] == COMPLETE:

            # 1 - Fetch translation content from 3Play Media.
            try:
                srt_transcript = fetch_srt_data(
                    url=THREE_PLAY_TRANSLATION_DOWNLOAD_URL.format(
                        file_id=translation_process.process_id, translation_id=translation_process.translation_id
                    ),
                    apikey=three_play_secrets.api_key,
                )
            except TranscriptFetchError:
                LOGGER.exception(
                    u'[3PlayMedia Task] Translation download failed for video=%s -- lang_code=%s -- process_id=%s.',
                    *log_args
                )
                continue

            # 2 - Validate the translation's SRT content received from 3Play Media.
            is_transcript_valid = validate_transcript_response(
                edx_video_id=translation_process.video.studio_id,
                file_id=translation_process.process_id,
                transcript=srt_transcript,
                lang_code=translation_process.lang_code,
                log_prefix=log_prefix
            )
            if is_transcript_valid:
                translation_process.update(status=TranscriptStatus.READY)
            else:
                translation_process.update(status=TranscriptStatus.FAILED)
                continue

            # 3 - Convert SRT translation to SJson format and upload it to S3.
            try:
                sjson_transcript = convert_srt_to_sjson(srt_transcript)
                sjson_file = upload_sjson_to_s3(CONFIG, sjson_transcript)
            except Exception:
                # in case of any exception, log and raise.
                LOGGER.exception(
                    u'[3PlayMedia Task] translation failed for video=%s -- lang_code=%s -- process_id=%s',
                    *log_args
                )
                raise

            # 4 Update edx-val with completed transcript information
            val_api = VALAPICall(video_proto=None, val_status=None)
            val_api.update_val_transcript(
                video_id=translation_process.video.studio_id,
                lang_code=translation_process.lang_code,
                name=sjson_file,
                transcript_format=TRANSCRIPT_SJSON,
                provider=TranscriptProvider.THREE_PLAY,
            )

            # 5 - if all the processes for this video are complete, update video status in edx-val
            # update transcript status for video in edx-val as well as edx-video-pipeline.
            video_jobs = TranscriptProcessMetadata.objects.filter(video__studio_id=translation_process.video.studio_id)
            if all(video_job.status == TranscriptStatus.READY for video_job in video_jobs):
                val_api.update_video_status(translation_process.video.studio_id, VideoStatus.TRANSCRIPT_READY)