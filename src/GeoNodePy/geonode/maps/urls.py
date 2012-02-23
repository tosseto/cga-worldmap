from django.conf.urls.defaults import *

js_info_dict = {
    'packages': ('geonode.maps',),
}

urlpatterns = patterns('geonode.maps.views',
    (r'^$', 'maps'),
    url(r'^new/?$', 'newmap', name="map_new"),
    url(r'^new/data$', 'newmapJSON'),
    (r'^(?P<mapid>\d+)/share/?$', 'map_share'),
    (r'^(?P<mapid>\d+)/edit/?$', 'map_controller'),
    (r'^(?P<mapid>\d+)/edit/describe/?$', 'describemap'),
    (r'^(?P<mapid>\d+)/download/?$', 'map_download'),
    (r'^check/?$', 'check_download'),
    (r'^checkurl/?$', 'ajax_url_lookup'),
    (r'^history/(?P<mapid>\d+)/?$', 'ajax_snapshot_history'),
    (r'^embed/?$', 'embed'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/embed/?$', 'embed'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/mobile/?$', 'mobilemap'),
    (r'^(?P<mapid>\d+)/data/?$', 'mapJSON'),
    (r'^addgeonodelayer/?$', 'addLayerJSON'),
    (r'^snapshot/create/?$', 'snapshot_create'),
    url(r'^search/?$', 'maps_search_page', name='maps_search'),
    url(r'^search/api/?$', 'maps_search', name='maps_search_api'),
    url(r'^(?P<mapid>\d+)/ajax-permissions/?$', 'ajax_map_permissions', name='ajax_map_permissions'),
    url(r'^change-poc/(?P<ids>\w+)/?$', 'change_poc', name="change_poc"),    
    url(r'^(?P<mapid>\d+)/ajax-permissions-email/?$', 'ajax_map_permissions_by_email', name='ajax_map_permissions_by_email'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/(?P<snapshot>\w+)/?$', 'view'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/(?P<snapshot>\w+)/embed/?$', 'embed'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/(?P<snapshot>\w+)/mobile/?$', 'mobilemap'),
    (r'^(?P<mapid>[A-Za-z0-9_\-]+)/?$', 'view'),
)

datapatterns = patterns('geonode.maps.views',
    url(r'^$', 'browse_data', name='data'),
    url(r'^acls/?$', 'layer_acls', name='layer_acls'),
    url(r'^search/?$', 'search_page', name='search'),
    url(r'^search/api/?$', 'metadata_search', name='search_api'),
    url(r'^search/detail/?$', 'search_result_detail', name='search_result_detail'),
    url(r'^api/batch_permissions/?$', 'batch_permissions'),
    url(r'^api/batch_delete/?$', 'batch_delete'),
    url(r'^upload/?', 'upload_layer', name='data_upload'),
    (r'^download$', 'batch_layer_download'),
    url(r'^(?P<layername>[^/]*)$', 'layer_detail', name="layer_detail"),
    url(r'^(?P<layername>[^/]*)/metadata$', 'layer_metadata', name="layer_metadata"),
    url(r'^(?P<layername>[^/]*)/remove$', 'layer_remove', name="layer_remove"),
    url(r'^(?P<layername>[^/]*)/replace$', 'layer_replace', name="layer_replace"),
    url(r'^(?P<layername>[^/]*)/style$', 'layer_style', name="layer_style"),
    (r'^(?P<layername>[^/]*)/ajax-permissions$', 'ajax_layer_permissions'),
    (r'^(?P<layername>[^/]*)/ajax-permissions-email$', 'ajax_layer_permissions_by_email'),
    (r'^(?P<layername>[^/]*)/ajax_layer_edit_check/?$', 'ajax_layer_edit_check'),
    (r'^(?P<layername>[^/]*)/ajax_layer_update_bounds/?$', 'ajax_layer_update_bounds'),
    (r'^layerstats/?$', 'ajax_increment_layer_stats'),
    url(r'^addlayers/?$', 'addlayers', name='addlayers'),
    url(r'^api/batch_permissions_by_email/?$', 'batch_permissions_by_email'),
    url(r'^create_pg_layer', 'create_pg_layer', name='create_pg_layer'),
)


