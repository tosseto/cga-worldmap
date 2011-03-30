from django.conf import settings

def resource_urls(request): 
    return dict(
        STATIC_URL = settings.STATIC_URL,
        GEONODE_CLIENT_LOCATION = settings.GEONODE_CLIENT_LOCATION,
        GEOSERVER_BASE_URL = settings.GEOSERVER_BASE_URL,
        GOOGLE_API_KEY = settings.GOOGLE_API_KEY,
        GOOGLE_ANALYTICS_CODE = settings.GOOGLE_ANALYTICS_CODE,
        SITENAME = settings.SITENAME,
        REGISTRATION_OPEN = settings.REGISTRATION_OPEN,
        CUSTOM_GROUP_NAME = settings.CUSTOM_GROUP_NAME if settings.USE_CUSTOM_ORG_AUTHORIZATION else '',
    )
