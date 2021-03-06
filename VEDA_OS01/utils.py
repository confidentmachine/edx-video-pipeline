"""
Common utils.
"""

from VEDA_OS01.models import TranscriptStatus


class ValTranscriptStatus(object):
    """
    VAL supported video transcript statuses.
    """
    TRANSCRIPTION_IN_PROGRESS = 'transcription_in_progress'
    TRANSCRIPT_READY = 'transcript_ready'


# Maps the edx-video-pipeline video transcript statuses to edx-val statuses.
VAL_TRANSCRIPT_STATUS_MAP = {
    TranscriptStatus.IN_PROGRESS: ValTranscriptStatus.TRANSCRIPTION_IN_PROGRESS,
    TranscriptStatus.READY: ValTranscriptStatus.TRANSCRIPT_READY
}


def update_video_status(val_api_client, video, status):
    """
    Updates video status both in edx-val and edx-video-pipeline.

    Arguments:
        video(Video): Video data model object
        status(Str): Video status to be updated
    """
    # update edx-val's video status
    try:
        val_status = VAL_TRANSCRIPT_STATUS_MAP[status]
        val_api_client.update_video_status(video.studio_id, val_status)
    except KeyError:
        # Don't update edx-val's video status.
        pass

    # update edx-video-pipeline's video status
    video.transcript_status = status
    video.save()


def invalidate_fernet_cached_properties(model, fields):
    """
    Invalidates transcript credential fernet field's cached properties.

    Arguments:
        model (class): Model class containing fernet fields.
        fields (list):  A list of fernet fields whose cache is to be invalidated.
    """
    for field_name in fields:
        try:
            field = model._meta.get_field(field_name)
            del field.keys
            del field.fernet_keys
            del field.fernet
        except AttributeError:
            pass
