import importlib
import logging
import json
import os
import io
import time
import zipfile
from zipfile import ZipFile, BadZipFile

from pywavefront import Wavefront

from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest, HttpResponseServerError, FileResponse
from django.shortcuts import get_object_or_404
from django.core.serializers import serialize
from django.core.serializers.json import DjangoJSONEncoder
from rest_framework import serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.permissions import IsAuthenticated

from .database import get_model_path, delete_model

DEFAULT_MAX_CHAR_LENGTH = 128

# Must use dynamic imports from 3dmr as valid python modules cannot start with a number.
ModelExtractor = getattr(
    importlib.import_module('third_party.3dmr.mainapp.model_extractor'),
    'ModelExtractor')

database = importlib.import_module('third_party.3dmr.mainapp.database')
models = importlib.import_module('third_party.3dmr.mainapp.models')
Model = getattr(models, 'Model')
LatestModel = getattr(models, 'LatestModel')
User = getattr(models, 'User')
MODEL_DIR = getattr(importlib.import_module('third_party.3dmr.mainapp.utils'), 'MODEL_DIR')

logger = logging.getLogger(__name__)

LICENSE_CHOICES = [
    (0, 'Creative Commons CC0 1.0 Universal Public Domain Dedication'),
    (1, 'Creative Commons Attribution 4.0 Internal license')
]


class ModelFileField(serializers.FileField):
    def to_internal_value(self, value):
        try:
            zip_file = ZipFile(value)
            logger.debug('inspecting zip file.')
            found_objs = 0 # files with the .obj extension found
            for name in zip_file.namelist():
                logger.debug('Found file with name: {}'.format(name))
                if name.endswith('.obj'):
                    found_objs += 1
            if found_objs != 1:
                logger.debug('No .obj file in upload.')
                raise serializers.ValidationError('No single .obj file found in your uploaded zip file.', code='invalid')

            with ModelExtractor(zip_file) as extracted_location:
                try:
                    scene = Wavefront(extracted_location['obj'])
                except:
                    logger.debug('Error parsing OBJ/MTL files.')
                    raise serializers.ValidationError('Error parsing OBJ/MTL files.', code='invalid')
        except BadZipFile:
            logger.debug('BadZipFile exception.')
            raise serializers.ValidationError('Uploaded file was not a valid zip file.', code='invalid')

        logger.debug('Custom zip validation successful.')
        return super().to_internal_value(value)


class ModelFileMetadataSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=DEFAULT_MAX_CHAR_LENGTH, default='')
    building_id = serializers.CharField(max_length=DEFAULT_MAX_CHAR_LENGTH, default='')
    description = serializers.CharField(max_length=1028, default='')
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    categories = serializers.CharField(max_length=1028, default='')
    tags = serializers.DictField(child=serializers.CharField(), default={})
    origin = serializers.ListField(child=serializers.FloatField(), default=[0., 0., 0.])
    translation = serializers.ListField(child=serializers.FloatField(), default=[0., 0., 0.])
    rotation = serializers.FloatField(default=0.0)
    scale = serializers.FloatField(default=1.0)
    license = serializers.ChoiceField(LICENSE_CHOICES, allow_blank=True)


class ModelFileSerializer(serializers.Serializer):
    model_file = ModelFileField()
    # For accepting model metadata as a json blob.
    # This is a workaround as Django Rest Framework struggles with multi-part posts
    # and must accept the file upload as a form post. Internally we'll de-serialize
    # the json field with the ModelFileMetadataSerializer
    metadata = serializers.JSONField(required=False)

class UserSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=DEFAULT_MAX_CHAR_LENGTH)
    password = serializers.CharField(write_only=True)
    email = serializers.EmailField(default='')

class DownloadBatchBuildingIdSerializer(serializers.Serializer):
    building_ids = serializers.ListField(child=serializers.CharField())

# For serializing model DB entries as JSON
class ModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Model
        depth = 1
        fields = [
            'model_id',
            'revision',
            'title',
            'building_id',
            'location',
            'rotation',
            'scale',
            'translation_x',
            'translation_y',
            'translation_z',
            'upload_date',
            'tags']

@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def delete(request):
    """API endpoint to delete a given model by ID.

    Example:
    curl -i -X POST -H 'Authorization: Token [insert token here]' \
    -H 'Content-Type: application/json'
    http://localhost:8080/api/v1/delete/
    -d '{"model_id": 12}'
    """

    try:
        data = json.loads(request.body)
        model_id = data['model_id']
    except:
        return HttpResponseBadRequest('Must provide model_id as json POST data.')

    logger.debug('{} requesets deletion of model id {}'.format(request.user.username, model_id))

    # There may be more than one revision associated with each model id.
    # However, each revision should belong to the original author.
    models = Model.objects.filter(model_id=model_id)
    for m in models:
        if request.user != m.author:
            return HttpResponseBadRequest('Must be author of the model to delete the model.')

    options = {
        'model_id': model_id,
    }

    if delete_model(options):
        logger.info('Deleted model id: {}'.format(data.get('model_id')))
    else:
        err_msg = 'Failed to delete model with id: {}'.format(data.get('model_id'))
        return HttpResponseServerError(err_msg)

    response_data = {
        'model_id': model_id,
        'status': 'deleted'
    }

    return JsonResponse(response_data,status=status.HTTP_202_ACCEPTED)

@api_view(['GET'])
def download_building_id(request, building_id, revision=None):
    models = Model.objects.filter(building_id = building_id).order_by('upload_date')

    if not models:
        logger.info('No models found matching building_id: {}'.format(building_id))
        return HttpResponse(status=status.HTTP_404_NOT_FOUND)

    logging.info('Found {} models with building_id {}'.format(len(models), building_id))

    m = models.last()
    if not revision:
        revision = get_object_or_404(LatestModel, model_id=m.model_id).revision
        logging.info('Revision was unspecified, found latest revision: {}'.format(revision))
    else:
        logging.info('Revision specified as {}'.format(revision))

    model_path = get_model_path(m.model_id, revision)
    logging.info('model_path: {}'.format(model_path))

    response = FileResponse(open(model_path, 'rb'))

    response['Content-Disposition'] = 'attachment; filename={}_{}.zip'.format(m.model_id, revision)
    response['Content-Type'] = 'application/zip'
    response['Cache-Control'] = 'public, max-age=86400'

    return response

    logging.error('Error reading model from disk: {}'.format(model_path))
    return HttpResponse(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
def download_batch_building_id(request):
    """API endpoint for downloading multiple models given a list of building ids.

    Example:
    curl -X -H "Content-Type: application/json" \
      -d '{"building_ids":["way/123", "way/456"]}' \
      http://localhost:8080/api/v1/download/batch/building_id/ \
      -o ~/Downloads/somefile.zip
    """
    start = time.perf_counter()
    logging.info('Batch download via building id list.')
    building_id_serializer = DownloadBatchBuildingIdSerializer(data=request.data)

    building_ids = []
    if building_id_serializer.is_valid():
        building_ids = building_id_serializer.data.get('building_ids')
        logging.debug(
            'Request for building_ids: {}'.format(building_ids))
    else:
        logging.error(
            'Unable to parse input building ids: {}'.format(request.data))
        return HttpResponseBadRequest()

    metadata = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'a', zipfile.ZIP_STORED, False) as zf:
        for model in Model.objects.filter(building_id__in=building_ids).order_by('upload_date'):
            model_id = model.model_id
            building_id = model.building_id
            if not metadata.get(model.building_id) and not model.is_hidden:
                latest_model = get_object_or_404(LatestModel, model_id=model_id)
                revision = latest_model.revision
                model_path = get_model_path(model_id, revision)

                # logging.debug(
                #     'building_id: {}, model_id: {}, revision: {}, model_path: {}'.format(
                #         building_id, model_id, revision, model_path))

                metadata[building_id] = json.loads(JSONRenderer().render(ModelSerializer(model).data))
                # logging.debug('metadata: {}'.format(metadata))

                filename = "{}.zip".format(building_id.replace('/','_'))
                metadata[building_id]['filename'] = filename


                zf.write(model_path, '{}.zip'.format(building_id.replace('/', '_')))
        zf.writestr('metadata.json', json.dumps(metadata))

    buf.seek(0)
    response = FileResponse(buf)
    response['Content-Disposition'] = 'attachment; filename=models.zip'
    response['Content-Type'] = 'application/zip'
    response['Cache-Control'] = 'public, max-age=86400'

    end = time.perf_counter()

    logging.debug(
        'Batch download of {} building ids completed in {} seconds.'.format(
            len(building_ids), end-start))

    return response

@api_view(['GET'])
def health(request):
    logger.debug('Health Check')
    logger.debug('request.META: {}'.format(request.META))
    return HttpResponse(status=status.HTTP_200_OK)

@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def new_token(request):
    """Request a new API token using the old token.

    Example:
    curl -i -X POST -H 'Authorization: Token [insert token here]' \
    http://localhost:8080/api/v1/new_token/
    """
    token = Token.objects.get(user=request.user)
    token.delete()
    token = Token.objects.create(user=request.user)

    context = {
        'username': request.user.username,
        'token': token.key
    }

    return JsonResponse(context, status=status.HTTP_202_ACCEPTED)

@api_view(['POST'])
def register(request):
    serialized = UserSerializer(data=request.data)

    if serialized.is_valid():
        logger.debug('data: {}'.format(serialized.data))
        user = User.objects.create_user(
            serialized.data.get('username'),
            serialized.data.get('password'),
            serialized.data.get('email')
        )

        user.save()

        token = Token.objects.get(user=user)

        response_data = {
            'username': serialized.data.get('username'),
            'email': serialized.data.get('email'),
            'token': token.key
        }

        logger.info('Created user: {} with token {}'.format(serialized.data.get('username'), token.key))

        return JsonResponse(response_data, status=status.HTTP_201_CREATED)
    else:
        logger.error('Failed to create user: {}'.format(serialized.data))
        return HttpResponseBadRequest(serialized._errors)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def revise(request, model_id):
    """Revise the binary blob associated with a model and increase the revision number.

    Example to modify model number 38:
    curl -i -X POST -F 'model_file=@/Some/path/to/a/model.zip' \
    -H 'Authorization: Token [some token]' \
    http://localhost:8080/api/v1/revise/38
    """
    logger.debug('Fetching model with id: {}'.format(model_id))

    try:
        m = LatestModel.objects.get(model_id=model_id)
    except:
        err_msg = 'Failed to find model with id: {}'.format(model_id)
        logger.warning(err_msg)
        return HttpResponseServerError(err_msg)

    old_revision = m.revision

    if request.user != m.author:
        err_msg = 'Must be author of the file to revise the model.'
        return HttpResponseBadRequest(err_msg)

    if not int(m.model_id) == int(model_id):
        err_msg = 'Server error: requested model_id: {}, fetched model_id: {}'.format(model_id, m.model_id)
        logger.error(err_msg)
        return HttpResponseServerError(err_msg)


    try:
        serialized_model = ModelFileSerializer(data=request.data)
    except:
        err_msg = 'Faild to deserialize model data revision.'
        logger.warning(err_msg)
        return HttpResponseBadRequest(err_msg)

    if serialized_model.is_valid():
        m = database.upload(serialized_model.validated_data.get('model_file'),
        {'revision': True,
         'model_id': model_id,
         'author': request.user})
    else:
        err_msg = 'Failed to validate model payload.'
        logger.warning(err_msg)
        return HttpResponseBadRequest(err_msg)

    response_data = {
        "model_id": model_id,
        "building_id": m.building_id,
        "old_revision":old_revision,
        "revision": m.revision
    }

    return JsonResponse(response_data,status=status.HTTP_202_ACCEPTED)

@api_view(['GET'])
def search_building_id(request, building_id):
    """Returns model ids matching a query building_id

    Example:
    curl -i -X GET http://localhost/api/v1/search/building_id/way/123/

    building_id is specified as a "path" converter in the url patterns so that
    any "/" characters will be included in the building_id excluding the terminating
    "/" character. In the above example, the building_id will be the string "way/123".
    """
    logging.debug('Searching for models with building_id: {}'.format(building_id))

    response_payload = {'models':[]}

    for model in Model.objects.filter(building_id = building_id).order_by('upload_date'):
        response_payload['models'].append(
            {'model_id': model.model_id,
             'title': model.title,
             'building_id': model.building_id,
             'revision': model.revision,
             'latitude': model.location.latitude,
             'longitude': model.location.longitude,
             'upload_date': model.upload_date,
             'tags': model.tags}
        )

    logging.debug('Found {} models matching building_id {}'.format(len(response_payload['models']), building_id))

    return JsonResponse(response_payload, safe=False, status=status.HTTP_200_OK)

@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def upload(request):
    """Api endpoint to upload a model zip file.

    Example:
    curl -i -X POST -F 'model_file=@/Some/path/to/models.zip' \
    -F 'latitude=38.1' -F 'longitude=2.1' -F 'license=0' -F 'tags=building_id=foo' \
    -H 'Authorization: Token [insert token here]' \
    http://localhost:8080/api/v1/upload/
    """

    logger.debug('{} requests file upload.'.format(request.user.username))

    try:
        serialized_model = ModelFileSerializer(data=request.data)
    except:
        import sys, traceback
        traceback.print_exc(file=sys.stdout)
        err_msg = 'Faild to validate model file upload request data.'
        logger.warning(err_msg)
        return HttpResponseBadRequest(err_msg)

    if serialized_model.is_valid():
        model_metadata = ModelFileMetadataSerializer(data=serialized_model.validated_data.get('metadata'))
    else:
        err_msg = 'Failed to validate model payload: {}'.format(serialized_model.errors)
        logger.warning(err_msg)
        return HttpResponseBadRequest(err_msg)

    if model_metadata.is_valid():
        logger.debug('Upload payload verified.')
        logger.debug('Validated meta data: {}'.format(model_metadata.validated_data))
        try:
            model = database.upload(serialized_model.validated_data.get('model_file'),
                                    {'title': model_metadata.validated_data.get('title'),
                                     'building_id': model_metadata.validated_data.get('building_id'),
                                     'description': model_metadata.validated_data.get('description'),
                                     'latitude': model_metadata.validated_data.get('latitude'),
                                     'longitude': model_metadata.validated_data.get('longitude'),
                                     'categories': model_metadata.validated_data.get('categories'),
                                     'tags': model_metadata.validated_data.get('tags'),
                                     'origin': model_metadata.validated_data.get('origin'),
                                     'translation': model_metadata.validated_data.get('translation'),
                                     'rotation': model_metadata.validated_data.get('rotation'),
                                     'scale': model_metadata.validated_data.get('scale'),
                                     'license':model_metadata.validated_data.get('license', None),
                                     'author':request.user})
        except:
            err_msg = 'Failed to upload model.'
            logger.warning(err_msg)
            return HttpResponseServerError(err_msg)

    else:
        err_msg = 'Failed to validate upload_model payload.'
        logger.warning(err_msg)
        return HttpResponseBadRequest(err_msg)

    response_data = {
        "model_id": model.model_id,
        "author": model.author.username,
        "revision": model.revision,
        "upload_date": model.upload_date,
    }

    return JsonResponse(response_data, safe=False, status=status.HTTP_201_CREATED)
