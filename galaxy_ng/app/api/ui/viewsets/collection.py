import galaxy_pulp

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django_filters import filters
from django_filters.rest_framework import filterset, DjangoFilterBackend, OrderingFilter
from drf_spectacular.utils import extend_schema
from pulp_ansible.app.galaxy.v3 import views as pulp_ansible_galaxy_views
from pulp_ansible.app.viewsets import CollectionVersionFilter
from pulp_ansible.app.models import (
    AnsibleDistribution,
    CollectionVersion,
    Collection,
    CollectionRemote,
)
from rest_framework.exceptions import NotFound
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from galaxy_ng.app import models
from galaxy_ng.app.api import base as api_base
from galaxy_ng.app.access_control import access_policy
from galaxy_ng.app.api.ui import serializers, versioning
from galaxy_ng.app.common import pulp
from galaxy_ng.app import constants


class CollectionViewSet(api_base.ViewSet):
    lookup_url_kwarg = 'collection'
    lookup_value_regex = r'[0-9a-z_]+/[0-9a-z_]+'
    permission_classes = [access_policy.CollectionAccessPolicy]
    versioning_class = versioning.UIVersioning

    def list(self, request, *args, **kwargs):
        self.paginator.init_from_request(request)

        params = {
            'offset': self.paginator.offset,
            'limit': self.paginator.limit,
        }
        for key, value in self.request.query_params.lists():
            if key == 'keywords':
                key = 'q'
            if isinstance(value, list):
                params[key] = ','.join(value)
            else:
                params[key] = value

        api = galaxy_pulp.PulpCollectionsApi(pulp.get_client())

        response = api.list(
            is_highest=True,
            exclude_fields='docs_blob',
            **params
        )

        namespaces = set(collection['namespace'] for collection in response.results)
        namespaces = self._query_namespaces(namespaces)

        data = serializers.CollectionListSerializer(
            response.results, many=True,
            context={'namespaces': namespaces}
        ).data
        return self.paginator.paginate_proxy_response(data, response.count)

    def retrieve(self, request, *args, **kwargs):
        namespace, name = self.kwargs['collection'].split('/')
        namespace_obj = get_object_or_404(models.Namespace, name=namespace)

        params_dict = self.request.query_params.dict()

        version = params_dict.get('version', '')

        api = galaxy_pulp.PulpCollectionsApi(pulp.get_client())

        params = {
            'namespace': namespace,
            'name': name,
        }

        if version == '':
            params['is_highest'] = True
            params['certification'] = constants.CertificationStatus.CERTIFIED.value
        else:
            params['version'] = version

        response = api.list(**params)

        if not response.results:
            raise NotFound()

        all_versions = api.list(
            namespace=namespace,
            name=name,
            fields='version,id,pulp_created,artifact',
            certification=constants.CertificationStatus.CERTIFIED.value
        )

        all_versions = [
            {
                'version': collection['version'],
                'id': collection['id'],
                'created': collection['pulp_created']
            } for collection in all_versions.results
        ]

        collection = response.results[0]

        data = serializers.CollectionDetailSerializer(
            collection,
            context={'namespace': namespace_obj, 'all_versions': all_versions}
        ).data

        return Response(data)

    @property
    def paginator(self):
        """
        The paginator instance associated with the view, or `None`.
        """
        if not hasattr(self, '_paginator'):
            if self.pagination_class is None:
                self._paginator = None
            else:
                self._paginator = self.pagination_class()
        return self._paginator

    @staticmethod
    def _query_namespaces(names):
        queryset = models.Namespace.objects.filter(name__in=names)
        namespaces = {ns.name: ns for ns in queryset}
        return namespaces


# TODO(awcrosby): After "ui/collections/" endpoints stop being used by UI,
# remove Collection*[ViewSet|Serializer] classes and replace with
# RepositoryCollection*[ViewSet|Serializer] classes


class RepositoryCollectionFilter(CollectionVersionFilter):
    versioning_class = versioning.UIVersioning
    keywords = filters.CharFilter(field_name="keywords", method="filter_by_q")


class RepositoryCollectionViewSet(pulp_ansible_galaxy_views.CollectionViewSet):
    """ Viewset that uses CollectionVersion to display data for Collection."""
    versioning_class = versioning.UIVersioning
    filterset_class = RepositoryCollectionFilter

    # TODO(awcrosby): remove once pulp_ansible is_highest param filters by repo
    # https://pulp.plan.io/issues/7428
    def get_queryset(self):
        """
        Overrides to not use is_highest param and
        ensures one collection version per collection is returned.
        """
        distro_content = self.get_distro_content(self.kwargs["path"])

        # isolate the use of distinct in separate database call to avoid
        # postgres error on use of distinct and order_by in later calls
        qs_distinct = CollectionVersion.objects.filter(
            pk__in=distro_content).distinct('collection')
        collection_version_pks = [cv.pk for cv in qs_distinct]

        qs = CollectionVersion.objects.select_related("collection").filter(
            pk__in=collection_version_pks)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return serializers.RepositoryCollectionListSerializer
        else:
            return serializers.RepositoryCollectionDetailSerializer


class CollectionVersionFilter(filterset.FilterSet):
    repository = filters.CharFilter(field_name='repository', method='repo_filter')
    versioning_class = versioning.UIVersioning

    def repo_filter(self, queryset, name, value):
        try:
            distro = AnsibleDistribution.objects.get(base_path=value)
            repository_version = distro.repository.latest_version()
            return queryset.filter(pk__in=repository_version.content)
        except ObjectDoesNotExist:
            return CollectionVersion.objects.none()

    sort = OrderingFilter(
        fields=(
            ('pulp_created', 'pulp_created'),
            ('namespace', 'namespace'),
            ('name', 'collection'),
            ('version', 'version'),
        )
    )

    class Meta:
        model = CollectionVersion
        fields = ['namespace', 'name', 'version', 'certification', 'repository']


class CollectionVersionViewSet(api_base.GenericViewSet):
    lookup_url_kwarg = 'version'
    lookup_value_regex = r'[0-9a-z_]+/[0-9a-z_]+/[0-9A-Za-z.+-]+'
    queryset = CollectionVersion.objects.all()
    serializer_class = serializers.CollectionVersionSerializer
    filterset_class = CollectionVersionFilter
    versioning_class = versioning.UIVersioning

    permission_classes = [access_policy.CollectionAccessPolicy]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @extend_schema(summary="Retrieve collection version",
                   responses={200: serializers.CollectionVersionDetailSerializer})
    def retrieve(self, request, *args, **kwargs):
        namespace, name, version = self.kwargs['version'].split('/')
        try:
            collection_version = CollectionVersion.objects.get(
                namespace=namespace,
                collection=Collection.objects.get(namespace=namespace, name=name),
                version=version,
            )
        except ObjectDoesNotExist:
            raise NotFound(f'Collection version not found for: {self.kwargs["version"]}')
        serializer = serializers.CollectionVersionDetailSerializer(collection_version)
        return Response(serializer.data)


class CollectionImportFilter(filterset.FilterSet):
    namespace = filters.CharFilter(field_name='namespace__name')
    created = filters.DateFilter(field_name='created_at')
    versioning_class = versioning.UIVersioning

    sort = OrderingFilter(
        fields=(('created_at', 'created'),)
    )

    class Meta:
        model = models.CollectionImport
        fields = ['namespace', 'name', 'version']


class CollectionImportViewSet(api_base.GenericViewSet):
    lookup_field = 'task_id'
    queryset = models.CollectionImport.objects.all()
    serializer_class = serializers.ImportTaskListSerializer

    filter_backends = [DjangoFilterBackend]
    filterset_class = CollectionImportFilter

    versioning_class = versioning.UIVersioning

    ordering_fields = ('created',)

    permission_classes = [access_policy.CollectionAccessPolicy]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)

        api = galaxy_pulp.GalaxyImportsApi(pulp.get_client())

        results = []
        for task in page:
            task_info = api.get(prefix=settings.X_PULP_API_PREFIX, id=str(task.pk))
            data = serializers.ImportTaskListSerializer(task_info, context={'task_obj': task}).data
            results.append(data)
        return self.get_paginated_response(results)

    @extend_schema(summary="Retrieve collection import",
                   responses={200: serializers.ImportTaskDetailSerializer})
    def retrieve(self, request, *args, **kwargs):
        api = galaxy_pulp.GalaxyImportsApi(pulp.get_client())
        task = self.get_object()
        task_info = api.get(prefix=settings.X_PULP_API_PREFIX, id=self.kwargs['task_id'])
        data = serializers.ImportTaskDetailSerializer(task_info, context={'task_obj': task}).data
        return Response(data)


class CollectionRemoteViewSet(api_base.ModelViewSet):
    queryset = CollectionRemote.objects.all().order_by('name')
    serializer_class = serializers.CollectionRemoteSerializer

    permission_classes = [access_policy.CollectionRemoteAccessPolicy]
