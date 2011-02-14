from geonode.core.models import AUTHENTICATED_USERS, ANONYMOUS_USERS, CUSTOM_GROUP_USERS
from geonode.maps.models import Map, Layer, MapLayer, LayerCategory, LayerAttribute, Contact, ContactRole,Role, get_csw
from geonode.maps.gs_helpers import fixup_style, cascading_delete

from geonode import geonetwork
import geoserver
from geoserver.resource import FeatureType, Coverage
import base64
from django import forms
from django.contrib.auth import authenticate, get_backends as get_auth_backends
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.conf import settings
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
import json
import math
import httplib2 
from owslib.csw import CswRecord, namespaces
from owslib.util import nspath
import re
from urllib import urlencode
from urlparse import urlparse
import uuid
import unicodedata
from django.views.decorators.csrf import csrf_exempt, csrf_response_exempt
from django.forms.models import inlineformset_factory
from django.db.models import Q
import logging
import datetime
from django.utils.encoding import iri_to_uri
from django.db.models.signals import pre_delete
from geonode.maps.models import delete_layer
from registration.models import RegistrationProfile
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.contrib.sites.models import Site  

logger = logging.getLogger("geonode.maps.views")

_user, _password = settings.GEOSERVER_CREDENTIALS


DEFAULT_TITLE = ""
DEFAULT_ABSTRACT = ""
DEFAULT_URL = ""

def _project_center(llcenter):
    wkt = "POINT({x} {y})".format(x=llcenter[0],y=llcenter[1])
    center = GEOSGeometry(wkt, srid=4326)
    center.transform("+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +wktext +no_defs")
    return center.x, center.y

_DEFAULT_MAP_CENTER = _project_center(settings.DEFAULT_MAP_CENTER)

_default_map = Map(
    title=DEFAULT_TITLE, 
    abstract=DEFAULT_ABSTRACT,
    urlsuffix=DEFAULT_URL,
    projection="EPSG:900913",
    center_x=_DEFAULT_MAP_CENTER[0],
    center_y=_DEFAULT_MAP_CENTER[1],
    zoom=settings.DEFAULT_MAP_ZOOM
)

def _baselayer(lyr, order):
    return MapLayer.objects.from_viewer_config(
        map = _default_map,
        layer = lyr,
        source = settings.MAP_BASELAYERSOURCES[lyr["source"]],
        ordering = order
    )

DEFAULT_BASELAYERS = [_baselayer(lyr, ord) for ord, lyr in enumerate(settings.MAP_BASELAYERS)]

DEFAULT_MAP_CONFIG = _default_map.viewer_json(*DEFAULT_BASELAYERS)

del _default_map
del _baselayer

def bbox_to_wkt(x0, x1, y0, y1, srid="4326"):
    return 'SRID='+srid+';POLYGON(('+x0+' '+y0+','+x0+' '+y1+','+x1+' '+y1+','+x1+' '+y0+','+x0+' '+y0+'))'
class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        exclude = ('user','is_org_member',)
        
class LayerCategoryForm(forms.ModelForm):
    class Meta:
        model = LayerCategory        


class LayerAttributeForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(LayerAttributeForm, self).__init__(*args, **kwargs)
        instance = getattr(self, 'instance', None)
        if instance and instance.attribute_type != 'xsd:string':
            self.fields['searchable'].widget.attrs['disabled'] = True
        self.fields['attribute'].widget.attrs['readonly'] = True    


    class Meta:
        model = LayerAttribute
        exclude = ('attribute_type',)

class LayerForm(forms.ModelForm):

    
#    topic_category = forms.ModelChoiceField(empty_label = "Create a new category", 
#                                label = "Topic Category", required=False,
#                                queryset = LayerCategory.objects.all())
#
#    topic_category_new = forms.CharField(label = "New Category", required=False, max_length=255)

    map_id = forms.CharField(widget=forms.HiddenInput(), initial='', required=False)


    date = forms.DateTimeField(widget=forms.SplitDateTimeWidget)
    date.widget.widgets[0].attrs = {"class":"date"}
    date.widget.widgets[1].attrs = {"class":"time"}
    temporal_extent_start = forms.DateField(required=False,widget=forms.DateInput(attrs={"class":"date"}))
    temporal_extent_end = forms.DateField(required=False,widget=forms.DateInput(attrs={"class":"date"}))

    
    
    poc = forms.ModelChoiceField(empty_label = "Person outside GeoNode (fill form)",
                                 label = "Point Of Contact", required=False,
                                 queryset = Contact.objects.exclude(user=None))

    metadata_author = forms.ModelChoiceField(empty_label = "Person outside GeoNode (fill form)",
                                             label = "Metadata Author", required=False,
                                             queryset = Contact.objects.exclude(user=None))
    


    class Meta:
        model = Layer
        exclude = ('contacts','workspace', 'store', 'name', 'uuid', 'storeType', 'typename') #, 'topic_category'

class RoleForm(forms.ModelForm):
    class Meta:
        model = ContactRole
        exclude = ('contact', 'layer')

class PocForm(forms.Form):
    contact = forms.ModelChoiceField(label = "New point of contact",
                                     queryset = Contact.objects.exclude(user=None))


class MapForm(forms.ModelForm):
    class Meta:
        model = Map
        exclude = ('contact', 'zoom', 'projection', 'center_x', 'center_y', 'owner', 'officialurl', 'urlsuffix', 'keywords', 'content', 'use_custom_template', 'group_params')
        widgets = {
            'abstract': forms.Textarea(attrs={'cols': 40, 'rows': 10}),
        }



MAP_LEV_NAMES = {
    Map.LEVEL_NONE  : _('No Permissions'),
    Map.LEVEL_READ  : _('Read Only'),
    Map.LEVEL_WRITE : _('Read/Write'),
    Map.LEVEL_ADMIN : _('Administrative')
}
LAYER_LEV_NAMES = {
    Layer.LEVEL_NONE  : _('No Permissions'),
    Layer.LEVEL_READ  : _('Read Only'),
    Layer.LEVEL_WRITE : _('Read/Write'),
    Layer.LEVEL_ADMIN : _('Administrative')
}

@transaction.commit_manually
def maps(request, mapid=None):
    logger.debug("STARTING MAPS VIEW")
    if request.method == 'GET':
        return render_to_response('maps.html', RequestContext(request))
    elif request.method == 'POST':
        if not request.user.is_authenticated():
            return HttpResponse(
                'You must be logged in to save new maps',
                mimetype="text/plain",
                status=401
            )
        try: 
            map = Map(owner=request.user, zoom=0, center_x=0, center_y=0)
            map.save()
            map.set_default_permissions()
            map.update_from_viewer(request.raw_post_data)
            response = HttpResponse('', status=201)
            response['Location'] = map.officialurl if map.officialurl else (map.urlsuffix if map.urlsuffix else map.id)
            transaction.commit()
            return response
        except Exception, e:
            transaction.rollback()
            return HttpResponse(
                "The server could not understand your request." + str(e),
                status=400, 
                mimetype="text/plain"
            )

def mapJSON(request, mapid):
    if request.method == 'GET':
        map = get_object_or_404(Map,pk=mapid) 
        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {})), status=401)
        return HttpResponse(json.dumps(map.viewer_json()))
    elif request.method == 'PUT':
        if not request.user.is_authenticated():
            return HttpResponse(
                _("You must be logged in to save this map"),
                status=401,
                mimetype="text/plain"
            )
        map = get_object_or_404(Map, pk=mapid)
        if not request.user.has_perm('maps.change_map', obj=map):
            return HttpResponse(
                "You do not have permission to save changes to this map.  Save a copy of the map instead.",
                mimetype="text/plain",
                status=403
            )  
        try:
            map.update_from_viewer(request.raw_post_data)
            return HttpResponse(
                "Map successfully updated.", 
                mimetype="text/plain",
                status=204
            )
        except Exception, e:
            return HttpResponse(
                "The server could not understand the request." + str(e),
                mimetype="text/plain",
                status=400
            )
@login_required
@csrf_exempt            
def newmap(request):
    '''
    View that creates a new map.  
    
    If the query argument 'copy' is given, the inital map is
    a copy of the map with the id specified, otherwise the 
    default map configuration is used.  If copy is specified
    and the map specified does not exist a 404 is returned.
    '''
    if request.method == 'GET' and 'copy' in request.GET:
        mapid = request.GET['copy']
        map = get_object_or_404(Map,pk=mapid)
        
        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to view or copy this map.")})), status=401)

        map.abstract = DEFAULT_ABSTRACT
        map.title = DEFAULT_TITLE
        map.urlsuffix = DEFAULT_URL
        if request.user.is_authenticated(): map.owner = request.user
        config = map.viewer_json()
        del config['id']
    else:
        if request.method == 'GET':
            params = request.GET
        elif request.method == 'POST':
            params = request.POST
        else:
            return HttpResponse(status=405)
        
        if 'layer' in params:
            bbox = None
            map = Map(projection="EPSG:900913")
            layers = []
            for layer_name in params.getlist('layer'):
                try:
                    layer = Layer.objects.get(typename=layer_name)
                except ObjectDoesNotExist:
                    # bad layer, skip 
                    continue

                if not request.user.has_perm('maps.view_layer', obj=layer):
                    # invisible layer, skip inclusion
                    continue
                    
                layer_bbox = layer.resource.latlon_bbox
                if bbox is None:
                    bbox = list(layer_bbox[0:4])
                else:
                    bbox[0] = min(bbox[0], layer_bbox[0])
                    bbox[1] = max(bbox[1], layer_bbox[1])
                    bbox[2] = min(bbox[2], layer_bbox[2])
                    bbox[3] = max(bbox[3], layer_bbox[3])
                
                layers.append(MapLayer(
                    map = map,
                    name = layer.typename,
                    ows_url = settings.GEOSERVER_BASE_URL + "wms",
                    visibility = True
                ))

            if bbox is not None:
                minx, maxx, miny, maxy = [float(c) for c in bbox]
                x = (minx + maxx) / 2
                y = (miny + maxy) / 2
                wkt = "POINT(" + str(x) + " " + str(y) + ")"
                center = GEOSGeometry(wkt, srid=4326)
                center.transform("+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +wktext +no_defs")

                width_zoom = math.log(360 / (maxx - minx), 2)
                height_zoom = math.log(360 / (maxy - miny), 2)

                map.center_x = center.x
                map.center_y = center.y
                map.zoom = math.ceil(min(width_zoom, height_zoom))

            config = map.viewer_json(*(DEFAULT_BASELAYERS + layers))
            config['fromLayer'] = True
        else:
            config = DEFAULT_MAP_CONFIG
            
        config['edit_map'] = True
    return render_to_response('maps/view.html', RequestContext(request, {
        'config': json.dumps(config), 
        'GOOGLE_API_KEY' : settings.GOOGLE_API_KEY,
        'GEOSERVER_BASE_URL' : settings.GEOSERVER_BASE_URL,
        'maptitle': settings.SITENAME
    }))

h = httplib2.Http()
h.add_credentials(_user, _password)
h.add_credentials(_user, _password)
_netloc = urlparse(settings.GEOSERVER_BASE_URL).netloc
h.authorizations.append(
    httplib2.BasicAuthentication(
        (_user, _password), 
        _netloc,
        settings.GEOSERVER_BASE_URL,
        {},
        None,
        None, 
        h
    )
)


@login_required
def map_download(request, mapid):
    """ 
    Download all the layers of a map as a batch
    XXX To do, remove layer status once progress id done 
    This should be fix because 
    """ 
    mapObject = get_object_or_404(Map,pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=mapObject):
        return HttpResponse(_('Not Permitted'), status=401)

    map_status = dict()
    if request.method == 'POST': 
        url = "%srest/process/batchDownload/launch/" % settings.GEOSERVER_BASE_URL

        def perm_filter(layer):
            return request.user.has_perm('maps.view_layer', obj=layer)

        mapJson = mapObject.json(perm_filter)

        resp, content = h.request(url, 'POST', body=mapJson)

        if resp.status not in (400, 404, 417):
            map_status = json.loads(content)
            request.session["map_status"] = map_status
        else: 
            pass # XXX fix

    if request.method == 'GET':
        if "map_status" in request.session and type(request.session["map_status"]) == dict:
            msg = "You already started downloading a map"
        else: 
            msg = "You should download a map" 

    locked_layers = []
    remote_layers = []
    downloadable_layers = []

    for lyr in mapObject.layer_set.all():
        if lyr.group != "background":
            if not lyr.local():
                remote_layers.append(lyr)
            else:
                ownable_layer = Layer.objects.get(typename=lyr.name)
                if not request.user.has_perm('maps.view_layer', obj=ownable_layer):
                    locked_layers.append(lyr)
                else:
                    downloadable_layers.append(lyr)

    return render_to_response('maps/download.html', RequestContext(request, {
         "map_status" : map_status,
         "map" : mapObject,
         "locked_layers": locked_layers,
         "remote_layers": remote_layers,
         "downloadable_layers": downloadable_layers,
         "geoserver" : settings.GEOSERVER_BASE_URL,
         "site" : settings.SITEURL
    }))
    

def check_download(request):
    """
    this is an endpoint for monitoring map downloads
    """
    try:
        layer = request.session["map_status"] 
        if type(layer) == dict:
            url = "%srest/process/batchDownload/status/%s" % (settings.GEOSERVER_BASE_URL,layer["id"])
            resp,content = h.request(url,'GET')
            status= resp.status
            if resp.status == 400:
                return HttpResponse(content="Something went wrong",status=status)
        else: 
            content = "Something Went wrong" 
            status  = 400 
    except ValueError:
        # TODO: Is there any useful context we could include in this log?
        logger.warn("User tried to check status, but has no download in progress.")
    return HttpResponse(content=content,status=status)


@csrf_exempt
def batch_layer_download(request):
    """
    batch download a set of layers
    
    POST - begin download
    GET?id=<download_id> monitor status
    """

    # currently this just piggy-backs on the map download backend 
    # by specifying an ad hoc map that contains all layers requested
    # for download. assumes all layers are hosted locally.
    # status monitoring is handled slightly differently.
    
    if request.method == 'POST':
        layers = request.POST.getlist("layer")
        layers = Layer.objects.filter(typename__in=list(layers))

        def layer_son(layer):
            return {
                "name" : layer.typename, 
                "service" : layer.service_type, 
                "metadataURL" : "",
                "serviceURL" : ""
            } 

        readme = """This data is provided by GeoNode.

Contents:
"""
        def list_item(lyr):
            return "%s - %s.*" % (lyr.title, lyr.name)

        readme = "\n".join([readme] + [list_item(l) for l in layers])

        fake_map = {
            "map": { "readme": readme },
            "layers" : [layer_son(lyr) for lyr in layers]
        }

        url = "%srest/process/batchDownload/launch/" % settings.GEOSERVER_BASE_URL
        resp, content = h.request(url,'POST',body=json.dumps(fake_map))
        return HttpResponse(content, status=resp.status)

    
    if request.method == 'GET':
        # essentially, this just proxies back to geoserver
        download_id = request.GET.get('id', None)
        if download_id is None:
            return HttpResponse(status=404)

        url = "%srest/process/batchDownload/status/%s" % (settings.GEOSERVER_BASE_URL, download_id)
        resp,content = h.request(url,'GET')
        return HttpResponse(content, status=resp.status)



def view_map_permissions(request, mapid):
    map = get_object_or_404(Map,pk=mapid) 

    if not request.user.has_perm('maps.change_map_permissions', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to view this map's permissions")})), status=401)

    ctx = _view_perms_context(map, MAP_LEV_NAMES)
    ctx['map'] = map
    return render_to_response("maps/permissions.html", RequestContext(request, ctx))


def ajax_layer_permissions(request, layername):
    layer = get_object_or_404(Layer, typename=layername)

    if not request.user.has_perm("maps.change_layer_permissions", obj=layer):
        return HttpResponse(
            'You are not allowed to change permissions for this layer',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing layer permissions',
            status=405,
            mimetype='text/plain'
        )
    if "customgroup" in request.POST:
        layer.set_gen_level(CUSTOM_GROUP_USERS, request.POST['customgroup'])
    elif "authenticated" in request.POST:
        layer.set_gen_level(AUTHENTICATED_USERS, request.POST['authenticated'])
    elif "anonymous" in request.POST:
        layer.set_gen_level(ANONYMOUS_USERS, request.POST['anonymous'])
    else:
        user_re = re.compile('^user\\.(.*)')
        for k, level in request.POST.iteritems():
            match = user_re.match(k)
            if match:
                username = match.groups()[0]
                user = User.objects.get(username=username)
                if level == '':
                    layer.set_user_level(user, layer.LEVEL_NONE)
                else:
                    layer.set_user_level(user, level)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )

def ajax_layer_permissions_by_email(request, layername):
    layer = get_object_or_404(Layer, typename=layername)

    if not request.user.has_perm("maps.change_layer_permissions", obj=layer):
        return HttpResponse(
            'You are not allowed to change permissions for this layer',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing layer permissions',
            status=405,
            mimetype='text/plain'
        )
    if "customgroup" in request.POST:
        layer.set_gen_level(CUSTOM_GROUP_USERS, request.POST['customgroup'])
    elif "authenticated" in request.POST:
        layer.set_gen_level(AUTHENTICATED_USERS, request.POST['authenticated'])
    elif "anonymous" in request.POST:
        layer.set_gen_level(ANONYMOUS_USERS, request.POST['anonymous'])
    else:
        user_re = re.compile('^user\\.(.*)')
        for k, level in request.POST.iteritems():
            match = user_re.match(k)
            if match:
                user_email = match.groups()[0]
                try:
                    user = User.objects.get(email=user_email)                    
                except User.DoesNotExist:
                    user = _create_new_user(user_email, layer.title, reverse('geonode.maps.views.layerController', args=(layer.typename,)), layer.owner_id)
                if level == '':
                    layer.set_user_level(user, layer.LEVEL_NONE)
                else:
                    layer.set_user_level(user, level)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )

def ajax_layer_edit_check(request, layername):
    layer = get_object_or_404(Layer, typename=layername);
    return HttpResponse(
            str(request.user.has_perm("maps.change_layer", obj=layer)),
            status=200,
            mimetype='text/plain'
        )


def ajax_map_edit_check_permissions(request, mapid):
    mapeditlevel = 'None'
    if not request.user.has_perm("maps.change_map_permissions", obj=map):

        return HttpResponse(
            'You are not allowed to change permissions for this map',
            status=401,
            mimetype='text/plain'
        )

def ajax_map_permissions(request, mapid):
    map = get_object_or_404(Map, pk=mapid)

    if not request.user.has_perm("maps.change_map_permissions", obj=map):
        return HttpResponse(
            'You are not allowed to change permissions for this map',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing map permissions',
            status=405,
            mimetype='text/plain'
        )

    if "customgroup" in request.POST:
        map.set_gen_level(CUSTOM_GROUP_USERS, request.POST['customgroup'])
    elif "authenticated" in request.POST:
        map.set_gen_level(AUTHENTICATED_USERS, request.POST['authenticated'])
    elif "anonymous" in request.POST:
        map.set_gen_level(ANONYMOUS_USERS, request.POST['anonymous'])
    else:
        user_re = re.compile('^user\\.(.*)')
        for k, level in request.POST.iteritems():
            match = user_re.match(k)
            if match:
                username = match.groups()[0]
                user = User.objects.get(username=username)
                if level == '':
                    map.set_user_level(user, map.LEVEL_NONE)
                else:
                    map.set_user_level(user, level)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )

def ajax_map_permissions_by_email(request, mapid):
    map = get_object_or_404(Map, pk=mapid)

    if not request.user.has_perm("maps.change_map_permissions", obj=map):
        return HttpResponse(
            'You are not allowed to change permissions for this map',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing map permissions',
            status=405,
            mimetype='text/plain'
        )

    if "customgroup" in request.POST:
        map.set_gen_level(CUSTOM_GROUP_USERS, request.POST['customgroup'])
    elif "authenticated" in request.POST:
        map.set_gen_level(AUTHENTICATED_USERS, request.POST['authenticated'])
    elif "anonymous" in request.POST:
        map.set_gen_level(ANONYMOUS_USERS, request.POST['anonymous'])
    else:
        user_re = re.compile('^user\\.(.*)')
        for k, level in request.POST.iteritems():
            match = user_re.match(k)
            if match:
                user_email = match.groups()[0]
                try:
                    user = User.objects.get(email=user_email)
                                     
                except User.DoesNotExist:
                    user = _create_new_user(user_email, map.title, reverse('geonode.maps.views.view', args=[map.id]), map.owner_id)
                if level == '':
                    map.set_user_level(user, map.LEVEL_NONE)
                else:
                    map.set_user_level(user, level)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )



def _create_new_user(user_email, map_layer_title, map_layer_url, map_layer_owner_id):
    
    random_password = User.objects.make_random_password()
    user_name = re.sub(r'\W', r'', user_email.split('@')[0])
    user_length = len(user_name)
    if user_length > 30:
        user_name = user_name[0:29]
    while len(User.objects.filter(username=user_name)) > 0:
        user_name = user_name[0:user_length-4] + User.objects.make_random_password(length=4, allowed_chars='0123456789')
    
    new_user = RegistrationProfile.objects.create_inactive_user(username=user_name, email=user_email, password=random_password, send_email=False)

    if new_user:
        new_profile = Contact(user=new_user, name=new_user.username, email=new_user.email)
        new_profile.save()

        _send_permissions_email(user_email, map_layer_title, map_layer_url, map_layer_owner_id, random_password)
    
    return new_user


def _send_permissions_email(user_email, map_layer_title, map_layer_url, map_layer_owner_id,  password):
      
    current_site = Site.objects.get_current()
    user = User.objects.get(email = user_email)
    profile = RegistrationProfile.objects.get(user=user)
    owner = User.objects.get(id=map_layer_owner_id)
    
    subject = render_to_string('registration/new_user_email_subject.txt',
                       { 'site': current_site,
                         'owner' : (owner.get_profile().name if owner.get_profile().name else owner.email),
                         })
    # Email subject *must not* contain newlines
    subject = ''.join(subject.splitlines())

    message = render_to_string('registration/new_user_email.txt',
                       { 'activation_key': profile.activation_key,
                         'expiration_days': settings.ACCOUNT_ACTIVATION_DAYS,
                         'owner': (owner.get_profile().name if owner.get_profile().name else owner.email),
                         'title': map_layer_title,
                         'url' : map_layer_url,                     
                         'site': current_site,
                         'username': user.username,
                         'password' : password })

    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email])

@login_required
def deletemap(request, mapid):
    ''' Delete a map, and its constituent layers. '''
    map = get_object_or_404(Map,pk=mapid) 

    if not request.user.has_perm('maps.delete_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to delete this map.")})), status=401)

    if request.method == 'GET':
        return render_to_response("maps/map_remove.html", RequestContext(request, {
            'map': map,
            'urlsuffix': get_suffix_if_custom(map)
        }))
    elif request.method == 'POST':
        layers = map.layer_set.all()
        for layer in layers:
            layer.delete()
        map.delete()

        return HttpResponseRedirect(reverse("geonode.maps.views.maps"))

def mapdetail(request,mapid): 
    '''
    The view that show details of each map
    '''
    map = get_object_or_404(Map,pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not allowed to view this map.")})), status=401)
     
    config = map.viewer_json()
    config = json.dumps(config)
    layers = MapLayer.objects.filter(map=map.id) 
    return render_to_response("maps/mapinfo.html", RequestContext(request, {
        'config': config, 
        'map': map,
        'layers': layers,
        'permissions_json': _perms_info_email_json(map, MAP_LEV_NAMES),
        'customGroup': settings.CUSTOM_GROUP_NAME,
        'urlsuffix':get_suffix_if_custom(map)
    }))

@csrf_exempt
@login_required
def describemap(request, mapid):
    '''
    The view that displays a form for
    editing map metadata
    '''
    map = get_object_or_404(Map,pk=mapid) 
    if not request.user.has_perm('maps.change_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
                            RequestContext(request, {'error_message': 
                            _("You are not allowed to modify this map's metadata.")})),
                            status=401)

    if request.method == "POST":
        # Change metadata, return to map info page
        map_form = MapForm(request.POST, instance=map, prefix="map")
        if map_form.is_valid():
            map_form.save()

            return HttpResponseRedirect(reverse('geonode.maps.views.map_controller', args=(map.id,)))
    else:
        # Show form
        map_form = MapForm(instance=map, prefix="map")

    return render_to_response("maps/map_describe.html", RequestContext(request, {
        "map": map,
        "map_form": map_form,
        "urlsuffix": get_suffix_if_custom(map)
    }))


def get_suffix_if_custom(map):
        if (map.use_custom_template):
            return map.officialurl
        else:
            return None

def map_controller(request, mapid):
    '''
    main view for map resources, dispatches to correct 
    view based on method and query args. 
    '''

    if mapid.isdigit():
        map = Map.objects.get(pk=mapid)
    else:
        map = Map.objects.get(urlsuffix=mapid)    
    
    if 'remove' in request.GET: 
        return deletemap(request, map.id)
    elif 'describe' in request.GET:
        return describemap(request, mapid)
    else:
        return mapdetail(request, map.id)

def official_site_controller(request, site):
    '''
    main view for map resources, dispatches to correct 
    view based on method and query args. 
    '''
    map = Map.objects.get(officialurl=site)
    return map_controller(request, str(map.id))



def view(request, mapid):
    """  
    The view that returns the map composer opened to
    the map with the given map ID.
    """
    if mapid.isdigit():
        map = Map.objects.get(pk=mapid)
    else:
        map = Map.objects.get(urlsuffix=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not allowed to view this map.")})), status=401)    
    
    config = map.viewer_json()
    logger.debug("CONFIG: [%s]", str(config))      
    
    first_visit = True
    if request.session.get('visit' + str(map.id), False):
        first_visit = False
    else:
        request.session['visit' + str(map.id)] = True
    
    #Remember last visited map
    request.session['lastmap'] = map.id
    request.session['lastmapTitle'] = map.title
            
    config['first_visit'] = first_visit
    config['edit_map'] = request.user.has_perm('maps.change_map', obj=map) 

    
    return render_to_response('maps/view.html', RequestContext(request, {
        'config': json.dumps(config),
        'GOOGLE_API_KEY' : settings.GOOGLE_API_KEY,
        'GEOSERVER_BASE_URL' : settings.GEOSERVER_BASE_URL,
        'maptitle': map.title,
        'urlsuffix': get_suffix_if_custom(map),
    }))


def official_site(request, site):
    """  
    The view that returns the map composer opened to
    the map with the given official site url.
    """
    map = Map.objects.get(officialurl=site)
    return view(request, str(map.id))

def embed(request, mapid=None):
    if mapid is None:
        config = DEFAULT_MAP_CONFIG
    else:

        if mapid.isdigit():
            map = Map.objects.get(pk=mapid)
        else:
            map = Map.objects.get(urlsuffix=mapid)\

        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(_("Not Permitted"), status=401, mimetype="text/plain")
        
        config = map.viewer_json()
    return render_to_response('maps/embed.html', RequestContext(request, {
        'config': json.dumps(config)
    }))


def data(request):
    return render_to_response('data.html', RequestContext(request, {
        'GEOSERVER_BASE_URL':settings.GEOSERVER_BASE_URL
    }))

def view_js(request, mapid):
    map = Map.objects.get(pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(_("Not Permitted"), status=401, mimetype="text/plain")
    config = map.viewer_json()
    return HttpResponse(json.dumps(config), mimetype="application/javascript")

def fixdate(str):
    return " ".join(str.split("T"))

class LayerDescriptionForm(forms.Form):
    title = forms.CharField(300)
    abstract = forms.CharField(1000, widget=forms.Textarea, required=False)
    keywords = forms.CharField(500, required=False)

@csrf_exempt
@login_required
def _describe_layer(request, layer):
    logger.debug("describe layer")
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.change_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to modify this layer's metadata")})), status=401)
        
        poc = layer.poc
        topic_category = layer.topic_category_id
        metadata_author = layer.metadata_author
        poc_role = ContactRole.objects.get(layer=layer, role=layer.poc_role)
        metadata_author_role = ContactRole.objects.get(layer=layer, role=layer.metadata_author_role)
        layerAttSet = inlineformset_factory(Layer, LayerAttribute, extra=0, form=LayerAttributeForm)

    
        
        if request.method == "GET":
            layer_form = LayerForm(instance=layer, prefix="layer")
            layer_form.fields["topic_category"].initial = topic_category
            if "map" in request.GET:
                layer_form.fields["map_id"].initial = request.GET["map"]
            attribute_form = layerAttSet(instance=layer, prefix="layer_attribute_set")


        if request.method == "POST":
            layer_form = LayerForm(request.POST, instance=layer, prefix="layer")
            attribute_form = layerAttSet(request.POST, instance=layer, prefix="layer_attribute_set")
            if layer_form.is_valid():
                if attribute_form.is_valid():
                    for form in attribute_form.cleaned_data:
                        la = LayerAttribute.objects.get(id=int(form['id'].id))
                        la.attribute_label = form["attribute_label"]
                        la.searchable = form["searchable"]
                        la.save()
            
                new_poc = layer_form.cleaned_data['poc']
                new_author = layer_form.cleaned_data['metadata_author']
                new_category = layer_form.cleaned_data['topic_category']
                mapid = layer_form.cleaned_data['map_id']
                logger.debug("map id is [%s]", mapid)


                if new_poc is None:
                    poc_form = ContactForm(request.POST, prefix="poc")
                    if poc_form.has_changed and poc_form.is_valid():
                        new_poc = poc_form.save()

                if new_author is None:
                    author_form = ContactForm(request.POST, prefix="author")
                    if author_form.has_changed and author_form.is_valid():
                        new_author = author_form.save()

                logger.debug("Save anything?")
                if new_poc is not None and new_author is not None:
                    the_layer = layer_form.save(commit=False)
                    the_layer.poc = new_poc
                    the_layer.topic_category = new_category
                    the_layer.metadata_author = new_author
                    logger.debug("About to save")
                    the_layer.save()
                    logger.debug("Saved")


                if request.is_ajax():
                    return HttpResponse('success', status=200)
                elif mapid != '':
                    logging.debug("adding layer to map [%s]", mapid)
                    maplayer = MapLayer.objects.create(map=Map.objects.get(id=mapid),
                        name = layer.typename,
                        group = layer.topic_category.title if layer.topic_category else 'General',
                        layer_params = '{"selected":true, "title": "' + layer.title + '"}',
                        source_params = '{"ptype": "gx_wmssource"}',
                        ows_url = settings.GEOSERVER_BASE_URL + "wms",
                        visibility = True,
                        stack_order = MapLayer.objects.filter(id=mapid).count()
                    )
                    maplayer.save()
                    return HttpResponseRedirect("/maps/" + mapid)
                else:
                    logger.debug("No map value found")
                    return HttpResponseRedirect("/data/" + layer.typename)




        if poc.user is None:
            poc_form = ContactForm(instance=poc, prefix="poc")
        else:
            layer_form.fields['poc'].initial = poc.id
            poc_form = ContactForm(prefix="poc")
            poc_form.hidden=True

        if metadata_author.user is None:
            author_form = ContactForm(instance=metadata_author, prefix="author")
        else:
            layer_form.fields['metadata_author'].initial = metadata_author.id
            author_form = ContactForm(prefix="author")
            author_form.hidden=True

        if request.method == 'POST' and not layer_form.is_valid() and request.is_ajax():
#                form_html = layer_form.as_ul()
#                data = {'form_html': form_html}
#                data = json.dumps(data)
                data = render_to_response("maps/layer_describe_tab.html", RequestContext(request, {
                "layer": layer,
                "layer_form": layer_form,
                "poc_form": poc_form,
                "author_form": author_form,
                "attribute_form": attribute_form,
                "lastmap" : request.session.get("lastmap"),
                "lastmapTitle" : request.session.get("lastmapTitle")
                }))
                return HttpResponse(data, status=412)

        if 'tab' in request.GET:
            return render_to_response("maps/layer_describe_tab.html", RequestContext(request, {
            "layer": layer,
            "layer_form": layer_form,
            "poc_form": poc_form,
            "author_form": author_form,
            "attribute_form": attribute_form,
            "lastmap" : request.session.get("lastmap"),
            "lastmapTitle" : request.session.get("lastmapTitle")
        }))


        return render_to_response("maps/layer_describe.html", RequestContext(request, {
            "layer": layer,
            "layer_form": layer_form,
            "poc_form": poc_form,
            "author_form": author_form,
            "attribute_form": attribute_form,
            "lastmap" : request.session.get("lastmap"),
            "lastmapTitle" : request.session.get("lastmapTitle")
        }))
    else:
        return HttpResponse("Not allowed", status=403)




@csrf_exempt
def _removeLayer(request,layer):
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.delete_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to delete this layer")})), status=401)
        
        if (request.method == 'GET'):
            return render_to_response('maps/layer_remove.html',RequestContext(request, {
                "layer": layer,
                "lastmap" : request.session.get("lastmap"),
                "lastmapTitle" : request.session.get("lastmapTitle") 
            }))
        if (request.method == 'POST'):
            layer.delete()
            return HttpResponseRedirect(reverse("data"))
        else:
            return HttpResponse("Not allowed",status=403) 
    else:  
        return HttpResponse("Not allowed",status=403)

@csrf_exempt
def _changeLayerDefaultStyle(request,layer):
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.change_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to modify this layer")})), status=401)
        
        if (request.method == 'POST'):
            style_name = request.POST.get('defaultStyle')

            # would be nice to implement
            # better handling of default style switching
            # in layer model or deeper (gsconfig.py, REST API)

            old_default = layer.default_style
            if old_default.name == style_name:
                return HttpResponse("Default style for %s remains %s" % (layer.name, style_name), status=200)

            # This code assumes without checking
            # that the new default style name is included
            # in the list of possible styles.

            new_style = (style for style in layer.styles if style.name == style_name).next()

            layer.default_style = new_style
            layer.styles = [s for s in layer.styles if s.name != style_name] + [old_default]
            layer.save()
            return HttpResponse("Default style for %s changed to %s" % (layer.name, style_name),status=200)
        else:
            return HttpResponse("Not allowed",status=403)
    else:  
        return HttpResponse("Not allowed",status=403)

@csrf_exempt
def layerController(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if ( "describe" in request.META['QUERY_STRING'] ):
        return _describe_layer(request,layer)
    if (request.META['QUERY_STRING'] == "remove"):
        return _removeLayer(request,layer)
    if (request.META['QUERY_STRING'] == "update"):
        return _updateLayer(request,layer)
    if (request.META['QUERY_STRING'] == "style"):
        return _changeLayerDefaultStyle(request,layer)
    else: 
        if not request.user.has_perm('maps.view_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to view this layer")})), status=401)
        
        metadata = layer.metadata_csw()
        maplayer = MapLayer(name = layer.typename, ows_url = settings.GEOSERVER_BASE_URL + "wms")

        # center/zoom don't matter; the viewer will center on the layer bounds
        map = Map(projection="EPSG:900913")

        return render_to_response('maps/layer.html', RequestContext(request, {
            "layer": layer,
            "metadata": metadata,
            "viewer": json.dumps(map.viewer_json(* (DEFAULT_BASELAYERS + [maplayer]))),
            "permissions_json": _perms_info_email_json(layer, LAYER_LEV_NAMES),
            "customGroup": settings.CUSTOM_GROUP_NAME,
            "GEOSERVER_BASE_URL": settings.GEOSERVER_BASE_URL,
            "lastmap" : request.session.get("lastmap"),
            "lastmapTitle" : request.session.get("lastmapTitle") 
        }))


GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")


@login_required
@csrf_exempt
def upload_layer(request):

    if request.method == 'GET':

        mapid = ''
        map = None
        if request.method == 'GET':
            if 'map' in request.GET:
                mapid = request.GET['map']
                map = get_object_or_404(Map,pk=mapid)
                return render_to_response('maps/layer_upload.html',
                                  RequestContext(request, {'map':map}))
            else: #this is a tabbed panel request if no map id provided
                return render_to_response('maps/layer_upload_tab.html',
                                  RequestContext(request))
    elif request.method == 'POST':
        try:
            logger.debug("Begin upload attempt")
            layer, errors = _handle_layer_upload(request)
            logger.debug("_handle_layer_upload returned. layer and errors are %s", (layer, errors))
            logger.debug("Save all attrbute names as searchable by defaul texcept geometry")
            try:
                #Add new layer attributes if they dont already exist
                if layer.attribute_names is not None:
                    logger.debug("Attributes are not None")
                    for field, ftype in layer.attribute_names.iteritems():
                        if re.search('geom|oid|objectid|gid', field, flags=re.I) is None:
                            logger.debug("Field is [%s]", field)
                            la = LayerAttribute.objects.create(layer=layer, attribute=field, attribute_label=field, attribute_type=ftype, searchable=(ftype == "xsd:string"))
                            la.save()
                else:
                    logger.debug("No attributes found")

            except:
                    logger.debug("Attributes could not be saved")
        except:
            logger.exception("_handle_layer_upload failed!")
            errors = [GENERIC_UPLOAD_ERROR]
        
        result = {}
        if len(errors) > 0:
            result['success'] = False
            result['errors'] = errors
        else:
            result['success'] = True
            result['redirect_to'] = reverse('geonode.maps.views.layerController', args=(layer.typename,)) + '?describe'
            if 'mapid' in request.POST and request.POST['mapid'] == 'tab':
               result['redirect_to'] += "&tab=true"
            elif 'mapid' in request.POST and request.POST['mapid'] != '':
                result['redirect_to'] += "&map=" + request.POST['mapid']

        result = json.dumps(result)
        logger.debug("layer upload - okay Django, you handle the rest.")
        return render_to_response('json_html.html',
                                  RequestContext(request, {'json': result}))


@login_required
@csrf_exempt
def _updateLayer(request, layer):
    if not request.user.has_perm('maps.change_layer', obj=layer):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to modify this layer")})), status=401)
    
    if request.method == 'GET':
        cat = Layer.objects.gs_catalog
        info = cat.get_resource(layer.name)
        is_featuretype = info.resource_type == FeatureType.resource_type
        
        return render_to_response('maps/layer_replace.html',
                                  RequestContext(request, {'layer': layer,
                                                           'is_featuretype': is_featuretype,
                                                           'lastmap' : request.session.get("lastmap"),
                                                           'lastmapTitle' : request.session.get("lastmapTitle")}))
    elif request.method == 'POST':
        try:
            layer, errors = _handle_layer_upload(request, layer=layer)
            try:
                #Delete layer attributes if they no longer exist in an updated layer
                for la in LayerAttribute.objects.filter(layer=layer):
                    lafound = False
                    if layer.attribute_names is not None:
                        for field, ftype in layer.attribute_names.iteritems():
                            if field == la.attribute:
                                lafound = True
                    if not lafound:
                        logger.debug("Going to delete [%s] for [%s]", la.attribute, layer.name)
                        la.delete()

                #Add new layer attributes if they dont already exist
                if layer.attribute_names is not None:
                    logger.debug("Attributes are not None")
                    for field, ftype in layer.attribute_names.iteritems():
                        if re.search('geom|oid|objectid|gid', field, flags=re.I) is None:
                            logger.debug("Field is [%s]", field)
                            las = LayerAttribute.objects.filter(layer=layer, attribute=field)
                            if len(las) == 0:
                                la = LayerAttribute.objects.create(layer=layer, attribute=field, attribute_label=field, attribute_type=ftype, searchable=(ftype == "xsd:string"))
                                la.save()
                else:
                    logger.debug("No attributes found")


            except Exception, ex:
                    logger.debug("Attributes could not be saved:[%s]", str(ex))


        except:
            errors = [GENERIC_UPLOAD_ERROR]

        result = {}
        if len(errors) > 0:
            result['success'] = False
            result['errors'] = errors
        else:
            result['success'] = True
            result['redirect_to'] = reverse('geonode.maps.views.layerController', args=(layer.typename,)) + "?describe"

    result = json.dumps(result)
    return render_to_response('json_html.html',
                              RequestContext(request, {'json': result}))

@transaction.commit_manually
def _handle_layer_upload(request, layer=None):
    """
    handle upload of layer data. if specified, the layer given is 
    overwritten, otherwise a new layer is created.
    """
    logger.debug("ENTER handle_layer_upload")
    layer_name = request.POST.get('layer_name');
    base_file = request.FILES.get('base_file');

    logger.info("Uploaded layer: [%s], base filename: [%s]", layer_name, base_file)

    if not base_file:
        logger.warn("Failed upload: no basefile provided")
        return None, [_("You must specify a layer data file to upload.")]
    
    if layer is None:
        overwrite = False
        # XXX Give feedback instead of just replacing name
        xml_unsafe = re.compile(r"(^[^a-zA-Z\._]+)|([^a-zA-Z\._0-9]+)")
        name = xml_unsafe.sub("_", layer_name)
        proposed_name = name
        count = 1
        while Layer.objects.filter(name=proposed_name).count() > 0:
            proposed_name = "%s_%d" % (name, count)
            count = count + 1
        name = proposed_name
        logger.info("Requested name already used; adjusting name [%s] => [%s]", layer_name, name)
    else:
        overwrite = True
        name = layer.name
        logger.info("Using name as requested")

    errors = []
    cat = Layer.objects.gs_catalog
    
    if not name:
        logger.error("Unexpected error: Layer name passed validation but is falsy: %s", name)
        return None, [_("Unable to determine layer name.")]

    # shapefile upload
    elif base_file.name.lower().endswith('.shp'):
        logger.info("Upload [%s] appears to be a Shapefile", base_file)
        # check that we are uploading the same resource 
        # type as the existing resource.
        if layer is not None:
            logger.info("Checking whether layer being replaced is a raster layer")
            info = cat.get_resource(name, store=cat.get_store(name))
            if info.resource_type != FeatureType.resource_type:
                logger.info("User tried to replace raster layer [%s] with Shapefile (vector) data", name)
                return None, [_("This resource may only be replaced with raster data.")]
        
        create_store = cat.create_featurestore
        dbf_file = request.FILES.get('dbf_file')
        shx_file = request.FILES.get('shx_file')
        prj_file = request.FILES.get('prj_file')
        
        if not dbf_file: 
            logger.info("User tried to upload [%s] without a .dbf file", base_file)
            errors.append(_("You must specify a .dbf file when uploading a shapefile."))
        if not shx_file: 
            logger.info("User tried to upload [%s] without a .shx file", base_file)
            errors.append(_("You must specify a .shx file when uploading a shapefile."))

        if not prj_file:
            logger.info("User tried to upload [%s] without a .prj file", base_file)

        if errors:
            return None, errors
        
        # ... bundle the files together and send them along
        cfg = {
            'shp': base_file,
            'dbf': dbf_file,
            'shx': shx_file
        }
        if prj_file:
            cfg['prj'] = prj_file

    # any other type of upload
    else:
        logger.info("Upload [%s] appears not to be a Shapefile", base_file)
        if layer is not None:
            logger.info("Checking whether replacement data for [%s] is raster", name)
            info = cat.get_resource(name, store=cat.get_store(name))
            if info.resource_type != Coverage.resource_type:
                logger.warn("User tried to replace vector layer [%s] with raster data", name)
                return [_("This resource may only be replaced with shapefile data.")]

        # ... we attempt to let geoserver figure it out, guessing it is coverage 
        create_store = cat.create_coveragestore
        cfg = base_file

    try:
        logger.debug("Starting upload of [%s] to GeoServer...", name)
        create_store(name, cfg, overwrite=overwrite)
        logger.debug("Finished upload of [%s] to GeoServer...", name)
    except geoserver.catalog.UploadError, e:
        logger.warn("Upload failed with error: %s", str(e))
        errors.append(_("An error occurred while loading the data."))
        tmp = cat.get_store(name)
        if tmp:
            logger.info("Deleting store after failed import of [%s] into GeoServer", name)
            cat.delete(tmp)
            logger.info("Successful deletion after failed import of [%s] into GeoServer", name)
    except geoserver.catalog.ConflictingDataError:
        errors.append(_("There is already a layer with the given name."))


    # if we successfully created the store in geoserver...
    if len(errors) == 0 and layer is None:
        logger.info("Succesful import of [%s] to GeoServer. Generating metadata", name)
        gs_resource = None
        csw_record = None
        layer = None
        try:
            gs_resource = cat.get_resource(name=name, store=cat.get_store(name=name))

            if gs_resource.latlon_bbox is None:
                cascading_delete(cat, gs_resource)
                logger.warn("GeoServer failed to detect the projection for layer [%s]. Cancelling import", name)
                errors.append(_("GeoNode could not detect the projection for %(layer)s.  Import is cancelled.") % { 'layer': name })
            else:
                typename = gs_resource.store.workspace.name + ':' + gs_resource.name
                logger.info("Got GeoServer info for %s, creating Django record", typename)

                # if we created a new store, create a new layer
                layer = Layer.objects.create(name=gs_resource.name, 
                                             store=gs_resource.store.name,
                                             storeType=gs_resource.store.resource_type,
                                             typename=typename,
                                             workspace=gs_resource.store.workspace.name,
                                             title=gs_resource.title,
                                             uuid=str(uuid.uuid4()),
                                             owner=request.user
                                           )
                # A user without a profile might be uploading this
                poc_contact, __ = Contact.objects.get_or_create(user=request.user,
                                                       defaults={"name": request.user.username })
                author_contact, __ = Contact.objects.get_or_create(user=request.user,
                                                       defaults={"name": request.user.username })
                logger.info("poc and author set to %s", poc_contact)
                layer.poc = poc_contact
                layer.metadata_author = author_contact
                logger.debug("committing DB changes for %s", typename)
                layer.save()
                logger.debug("Setting default permissions for %s", typename)
                layer.set_default_permissions()
                logger.debug("Generating separate style for %s", typename)
                fixup_style(cat, gs_resource)
        except Exception, e:
            logger.exception("Import to Django and GeoNetwork failed: %s", str(e))
            transaction.rollback()
            # Something went wrong, let's try and back out any changes
            if gs_resource is not None:
                logger.warning("no explicit link from the resource to [%s], bah", name)
                gs_layer = cat.get_layer(gs_resource.name) 
                store = gs_resource.store
                try:
                    cat.delete(gs_layer)
                except:
                    pass

                try: 
                    cat.delete(gs_resource)
                except:
                    pass

                try: 
                    cat.delete(store)
                except:
                    pass
            if csw_record is not None:
                logger.warning("Deleting dangling GeoNetwork record for [%s] (no Django record to match)", name)
                try:
                    gn.delete(csw_record)
                except:
                    pass
            # set layer to None, but we'll rely on db transactions instead
            # of a manual delete to keep it out of the db
            layer = None
            logger.warning("Finished cleanup after failed GeoNetwork/Django import for layer: %s", name)
            errors.append(GENERIC_UPLOAD_ERROR)
        else:
            transaction.commit()

    return layer, errors



@login_required
def view_layer_permissions(request, layername):
    layer = get_object_or_404(Layer,typename=layername) 

    if not request.user.has_perm('maps.change_layer_permissions', obj=layer):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to view this layer's permissions")})), status=401)
    
    ctx = _view_perms_context(layer, LAYER_LEV_NAMES)
    ctx['layer'] = layer
    return render_to_response("maps/layer_permissions.html", RequestContext(request, ctx))

def _view_perms_context(obj, level_names):

    ctx =  obj.get_all_level_info()
    def lname(l):
        return level_names.get(l, _("???"))
    ctx[ANONYMOUS_USERS] = lname(ctx.get(ANONYMOUS_USERS, obj.LEVEL_NONE))
    ctx[AUTHENTICATED_USERS] = lname(ctx.get(AUTHENTICATED_USERS, obj.LEVEL_NONE))
    ctx[CUSTOM_GROUP_USERS] = lname(ctx.get(CUSTOM_GROUP_USERS, obj.LEVEL_NONE))

    ulevs = []
    for u, l in ctx['users'].items():
        ulevs.append([u, lname(l)])
    ulevs.sort()
    ctx['users'] = ulevs

    return ctx

def _perms_info_json(obj, level_names):
    info = obj.get_all_level_info()
    # these are always specified even if none
    info[ANONYMOUS_USERS] = info.get(ANONYMOUS_USERS, obj.LEVEL_NONE)
    info[AUTHENTICATED_USERS] = info.get(AUTHENTICATED_USERS, obj.LEVEL_NONE)
    info[CUSTOM_GROUP_USERS] = info.get(CUSTOM_GROUP_USERS, obj.LEVEL_NONE)
    info['users'] = sorted(info['users'].items())
    info['levels'] = [(i, level_names[i]) for i in obj.permission_levels]
    if hasattr(obj, 'owner') and obj.owner: 
        info['owner'] = obj.owner.username
        info['owner_email'] = obj.owner.email
    return json.dumps(info)

def _perms_info_email_json(obj, level_names):
    info = obj.get_all_level_info_by_email()
    # these are always specified even if none
    info[ANONYMOUS_USERS] = info.get(ANONYMOUS_USERS, obj.LEVEL_NONE)
    info[AUTHENTICATED_USERS] = info.get(AUTHENTICATED_USERS, obj.LEVEL_NONE)
    info[CUSTOM_GROUP_USERS] = info.get(CUSTOM_GROUP_USERS, obj.LEVEL_NONE)
    info['users'] = sorted(info['users'].items())
    info['levels'] = [(i, level_names[i]) for i in obj.permission_levels]
    if hasattr(obj, 'owner') and obj.owner: 
        info['owner'] = obj.owner.username
        info['owner_email'] = obj.owner.email
    logging.debug(str(info))
    return json.dumps(info)

INVALID_PERMISSION_MESSAGE = _("Invalid permission level.")
def _handle_perms_edit(request, obj):
    errors = []
    params = request.POST
    valid_pl = obj.permission_levels
    
    anon_level = params[ANONYMOUS_USERS]
    # validate anonymous level, disallow admin level
    if not anon_level in valid_pl or anon_level == obj.LEVEL_ADMIN:
        errors.append(_("Anonymous Users") + ": " + INVALID_PERMISSION_MESSAGE)
    
    all_auth_level = params[AUTHENTICATED_USERS]
    if not all_auth_level in valid_pl:
        errors.append(_("Registered Users") + ": " + INVALID_PERMISSION_MESSAGE)

    customgroup_level = params[CUSTOM_GROUP_USERS]
    if not customgroup_level in valid_pl:
        errors.append(_("Custom Group Users") + ": " + INVALID_PERMISSION_MESSAGE)
        
    kpat = re.compile("^u_(.*)_level$")
    ulevs = {}
    for k, level in params.items(): 
        m = kpat.match(k)
        if m: 
            username = m.groups()[0]
            if not level in valid_pl:
                errors.append(_("User") + " " + username + ": " + INVALID_PERMISSION_MESSAGE)
            else:
                ulevs[username] = level

    if len(errors) == 0: 
        obj.set_gen_level(ANONYMOUS_USERS, anon_level)
        obj.set_gen_level(AUTHENTICATED_USERS, all_auth_level)
        obj.set_gen_level(CUSTOM_GROUP_USERS, customgroup_level)
        
        for username, level in ulevs.items():
            user = User.objects.get(username=username)
            obj.set_user_level(user, level)

    return errors


def _get_basic_auth_info(request):
    """
    grab basic auth info
    """
    meth, auth = request.META['HTTP_AUTHORIZATION'].split()
    if meth.lower() != 'basic':
        raise ValueError
    username, password = base64.b64decode(auth).split(':')
    return username, password

def layer_acls(request):
    """
    returns json-encoded lists of layer identifiers that 
    represent the sets of read-write and read-only layers
    for the currently authenticated user. 
    """
    
    # the layer_acls view supports basic auth, and a special 
    # user which represents the geoserver administrator that
    # is not present in django.
    #logger.debug("Entered layer_acls")
    acl_user = request.user
    #logger.debug("USER IS " + request.user)
    if 'HTTP_AUTHORIZATION' in request.META:
        #logger.debug("HTTP_AUTHORIZATION...")
        try:
            username, password = _get_basic_auth_info(request)
            #logger.debug("UserName: " + username + ",  PASS: " + password)
            acl_user = authenticate(username=username, password=password)

            #logger.debug("Geoserver creds: " + settings.GEOSERVER_CREDENTIALS[0] + ":" + settings.GEOSERVER_CREDENTIALS[1])

            # Nope, is it the special geoserver user?
            if (acl_user is None and 
                username == settings.GEOSERVER_CREDENTIALS[0] and
                password == settings.GEOSERVER_CREDENTIALS[1]):
                # great, tell geoserver it's an admin.
                #logger.debug("Geoserver admin logging on")
                result = {
                   'rw': [],
                   'ro': [],
                   'name': username,
                   'is_superuser':  True,
                   'is_anonymous': False
                }
                jsonResult = json.dumps(result)
                #logger.debug("Returning geoserver admin acls")
                return HttpResponse(jsonResult, mimetype="application/json")
        except:
            logger.debug("An error occurred while trying to authorize")
            pass
        
        if acl_user is None: 
            return HttpResponse(_("Bad HTTP Authorization Credentials."),
                                status=401,
                                mimetype="text/plain")
    else:
        'HTTP AUTHORIZATION NOT IN request!'
        
    logger.debug("Done with authorization check, creating json response")        
    all_readable = set()
    all_writable = set()
    for bck in get_auth_backends():
        if hasattr(bck, 'objects_with_perm'):
            all_readable.update(bck.objects_with_perm(acl_user,
                                                      'maps.view_layer',
                                                      Layer))
            all_writable.update(bck.objects_with_perm(acl_user,
                                                      'maps.change_layer', 
                                                      Layer))
    read_only = [x for x in all_readable if x not in all_writable]
    read_write = [x for x in all_writable if x in all_readable]

    read_only = [x[0] for x in Layer.objects.filter(id__in=read_only).values_list('typename').all()]
    read_write = [x[0] for x in Layer.objects.filter(id__in=read_write).values_list('typename').all()]
    
    result = {
        'rw': read_write,
        'ro': read_only,
        'name': acl_user.username,
        'is_superuser':  acl_user.is_superuser,
        'is_anonymous': acl_user.is_anonymous()
    }

    return HttpResponse(json.dumps(result), mimetype="application/json")


def _split_query(query):
    """
    split and strip keywords, preserve space 
    separated quoted blocks.
    """

    qq = query.split(' ')
    keywords = []
    accum = None
    for kw in qq: 
        if accum is None: 
            if kw.startswith('"'):
                accum = kw[1:]
            elif kw: 
                keywords.append(kw)
        else:
            accum += ' ' + kw
            if kw.endswith('"'):
                keywords.append(accum[0:-1])
                accum = None
    if accum is not None:
        keywords.append(accum)
    return [kw.strip() for kw in keywords if kw.strip()]



DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
@csrf_exempt
def metadata_search(request):
    """
    handles a basic search for data using the 
    GeoNetwork catalog.

    the search accepts: 
    q - general query for keywords across all fields
    start - skip to this point in the results
    limit - max records to return

    for ajax requests, the search returns a json structure 
    like this: 
    
    {
    'total': <total result count>,
    'next': <url for next batch if exists>,
    'prev': <url for previous batch if exists>,
    'query_info': {
        'start': <integer indicating where this batch starts>,
        'limit': <integer indicating the batch size used>,
        'q': <keywords used to query>,
    },
    'rows': [
      {
        'name': <typename>,
        'abstract': '...',
        'keywords': ['foo', ...],
        'detail' = <link to geonode detail page>,
        'attribution': {
            'title': <language neutral attribution>,
            'href': <url>
        },
        'download_links': [
            ['pdf', 'PDF', <url>],
            ['kml', 'KML', <url>],
            [<format>, <name>, <url>]
            ...
        ],
        'metadata_links': [
           ['text/xml', 'TC211', <url>],
           [<mime>, <name>, <url>],
           ...
        ]
      },
      ...
    ]}
    """
    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    # grab params directly to implement defaults as
    # opposed to panicy django forms behavior.
    query = params.get('q', '')
    try:
        start = int(params.get('start', '0'))
    except:
        start = 0
    try:
        limit = min(int(params.get('limit', DEFAULT_SEARCH_BATCH_SIZE)),
                    MAX_SEARCH_BATCH_SIZE)
    except: 
        limit = DEFAULT_SEARCH_BATCH_SIZE

    advanced = {}
    bbox = params.get('bbox', None)
    if bbox:
        try:
            bbox = [float(x) for x in bbox.split(',')]
            if len(bbox) == 4:
                advanced['bbox'] =  bbox
        except:
            # ignore...
            pass

    result = _metadata_search(query, start, limit, **advanced)

    # XXX slowdown here to dig out result permissions
    for doc in result['rows']: 
        try: 
            layer = Layer.objects.get(uuid=doc['uuid'])
            doc['_local'] = True
            doc['_permissions'] = {
                'view': request.user.has_perm('maps.view_layer', obj=layer),
                'change': request.user.has_perm('maps.change_layer', obj=layer),
                'delete': request.user.has_perm('maps.delete_layer', obj=layer),
                'change_permissions': request.user.has_perm('maps.change_layer_permissions', obj=layer),
            }
        except Layer.DoesNotExist:
            doc['_local'] = False
            pass

    result['success'] = True
    return HttpResponse(json.dumps(result), mimetype="application/json")

def _metadata_search(query, start, limit, **kw):
    
    csw = get_csw()

    keywords = _split_query(query)
    
    csw.getrecords(keywords=keywords, startposition=start+1, maxrecords=limit, bbox=kw.get('bbox', None))
    
    
    # build results 
    # XXX this goes directly to the result xml doc to obtain 
    # correct ordering and a fuller view of the result record
    # than owslib currently parses.  This could be improved by
    # improving owslib.
    results = [_build_search_result(doc) for doc in 
               csw._exml.findall('//'+nspath('Record', namespaces['csw']))]

    result = {'rows': results, 
              'total': csw.results['matches']}

    result['query_info'] = {
        'start': start,
        'limit': limit,
        'q': query
    }
    if start > 0: 
        prev = max(start - limit, 0)
        params = urlencode({'q': query, 'start': prev, 'limit': limit})
        result['prev'] = reverse('geonode.maps.views.metadata_search') + '?' + params

    next = csw.results.get('nextrecord', 0) 
    if next > 0:
        params = urlencode({'q': query, 'start': next - 1, 'limit': limit})
        result['next'] = reverse('geonode.maps.views.metadata_search') + '?' + params
    
    return result

def search_result_detail(request):
    uuid = request.GET.get("uuid")
    csw = get_csw()
    csw.getrecordbyid([uuid], outputschema=namespaces['gmd'])
    rec = csw.records.values()[0]
    raw_xml = csw._exml.find(nspath('MD_Metadata', namespaces['gmd']))
    extra_links = _extract_links(rec, raw_xml)
    category = ''
    
    try:
        layer = Layer.objects.get(uuid=uuid)
        layer_is_remote = False
        category = layer.topic_category
    except:
        layer = None
        layer_is_remote = True
        
    return render_to_response('maps/search_result_snippet.html', RequestContext(request, {
        'rec': rec,
        'extra_links': extra_links,
        'layer': layer,
        'layer_is_remote': layer_is_remote,
        'category' : category
    }))

def _extract_links(rec, xml):
    download_links = []
    dl_type_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("protocol", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_name_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("name", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_description_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("description", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_link_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("linkage", namespaces["gmd"]),
        nspath("URL", namespaces["gmd"])
        ])

    format_re = re.compile(".*\((.*)(\s*Format*\s*)\).*?")

    for link in xml.findall("*//" + nspath("onLine", namespaces['gmd'])):
        if link.find(dl_type_path).text == "WWW:DOWNLOAD-1.0-http--download":
            extension = link.find(dl_name_path).text.split('.')[-1]
            format = format_re.match(link.find(dl_description_path).text).groups()[0]
            url = link.find(dl_link_path).text
            download_links.append((extension, format, url))
    return dict(
            download=download_links
        )


def _build_search_result(doc):
    """
    accepts a node representing a csw result 
    record and builds a POD structure representing 
    the search result.
    """
    if doc is None:
        return None
    # Let owslib do some parsing for us...
    rec = CswRecord(doc)
    result = {}
    result['title'] = rec.title
    result['uuid'] = rec.identifier
    result['abstract'] = rec.abstract
    result['keywords'] = [x for x in rec.subjects if x]
    result['detail'] = rec.uri or ''

    # XXX needs indexing ? how
    result['attribution'] = {'title': '', 'href': ''}

    # XXX !_! pull out geonode 'typename' if there is one
    # index this directly... 
    if rec.uri:
        try:
            result['name'] = urlparse(rec.uri).path.split('/')[-1]
        except: 
            pass
    # fallback: use geonetwork uuid
    if not result.get('name', ''):
        result['name'] = rec.identifier

    # Take BBOX from GeoNetwork Result...
    # XXX this assumes all our bboxes are in this 
    # improperly specified SRS.
    if rec.bbox is not None and rec.bbox.crs == 'urn:ogc:def:crs:::WGS 1984':
        # slight workaround for ticket 530
        result['bbox'] = {
            'minx': min(rec.bbox.minx, rec.bbox.maxx),
            'maxx': max(rec.bbox.minx, rec.bbox.maxx),
            'miny': min(rec.bbox.miny, rec.bbox.maxy),
            'maxy': max(rec.bbox.miny, rec.bbox.maxy)
        }
    
    # XXX these could be exposed in owslib record...
    # locate all download links
    format_re = re.compile(".*\((.*)(\s*Format*\s*)\).*?")
    result['download_links'] = []
    for link_el in doc.findall(nspath('URI', namespaces['dc'])):
        if link_el.get('protocol', '') == 'WWW:DOWNLOAD-1.0-http--download':
            try:
                extension = link_el.get('name', '').split('.')[-1]
                format = format_re.match(link_el.get('description')).groups()[0]
                href = link_el.text
                result['download_links'].append((extension, format, href))
            except: 
                pass

    # construct the link to the geonetwork metadata record (not self-indexed)
    md_link = settings.GEONETWORK_BASE_URL + "srv/en/csw?" + urlencode({
            "request": "GetRecordById",
            "service": "CSW",
            "version": "2.0.2",
            "OutputSchema": "http://www.isotc211.org/2005/gmd",
            "ElementSetName": "full",
            "id": rec.identifier
        })
    result['metadata_links'] = [("text/xml", "TC211", md_link)]

    return result

def browse_data(request):
    return render_to_response('data.html', RequestContext(request, {}))

@csrf_exempt    
def search_page(request):
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    map = Map(projection="EPSG:900913", zoom = 1, center_x = 0, center_y = 0)

    return render_to_response('search.html', RequestContext(request, {
        'init_search': json.dumps(params or {}),
        'viewer_config': json.dumps(map.viewer_json(*DEFAULT_BASELAYERS)),
        'GOOGLE_API_KEY' : settings.GOOGLE_API_KEY,
        "site" : settings.SITEURL
    }))



def addlayers(request):
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    map = Map(projection="EPSG:900913", zoom = 1, center_x = 0, center_y = 0)

    return render_to_response('addlayers.html', RequestContext(request, {
        'init_search': json.dumps(params or {}),
        'viewer_config': json.dumps(map.viewer_json(*DEFAULT_BASELAYERS)),
        'GOOGLE_API_KEY' : settings.GOOGLE_API_KEY,
        "site" : settings.SITEURL
    }))


def change_poc(request, ids, template = 'maps/change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            return HttpResponseRedirect('/admin/maps/layer') # Redirect after POST
    else:
        form = PocForm() # An unbound form
    return render_to_response(template, RequestContext(request, 
                                  {'layers': layers, 'form': form }))


#### MAPS SEARCHING ####

DEFAULT_MAPS_SEARCH_BATCH_SIZE = 10
MAX_MAPS_SEARCH_BATCH_SIZE = 25
@csrf_exempt
def maps_search(request):
    """
    handles a basic search for maps using the 
    GeoNetwork catalog.

    the search accepts: 
    q - general query for keywords across all fields
    start - skip to this point in the results
    limit - max records to return
    sort - field to sort results on
    dir - ASC or DESC, for ascending or descending order

    for ajax requests, the search returns a json structure 
    like this: 
    
    {
    'total': <total result count>,
    'next': <url for next batch if exists>,
    'prev': <url for previous batch if exists>,
    'query_info': {
        'start': <integer indicating where this batch starts>,
        'limit': <integer indicating the batch size used>,
        'q': <keywords used to query>,
    },
    'rows': [
      {
        'title': <map title,
        'abstract': '...',
        'detail' : <url geonode detail page>,
        'owner': <name of the map's owner>,
        'owner_detail': <url of owner's profile page>,
        'last_modified': <date and time of last modification>
      },
      ...
    ]}
    """
    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    # grab params directly to implement defaults as
    # opposed to panicy django forms behavior.
    query = params.get('q', '')
    try:
        start = int(params.get('start', '0'))
    except:
        start = 0
    try:
        limit = min(int(params.get('limit', DEFAULT_MAPS_SEARCH_BATCH_SIZE)),
                    MAX_MAPS_SEARCH_BATCH_SIZE)
    except: 
        limit = DEFAULT_MAPS_SEARCH_BATCH_SIZE


    sort_field = params.get('sort', u'')
    sort_field = unicodedata.normalize('NFKD', sort_field).encode('ascii','ignore')  
    sort_dir = params.get('dir', 'ASC')
    result = _maps_search(query, start, limit, sort_field, sort_dir)

    result['success'] = True
    return HttpResponse(json.dumps(result), mimetype="application/json")

def _maps_search(query, start, limit, sort_field, sort_dir):

    keywords = _split_query(query)
 
    if len(keywords) == 0:
        maps = Map.objects.all()

    maps = Map.objects
    for keyword in keywords:
        maps = maps.filter(
              Q(title__icontains=keyword)
            | Q(abstract__icontains=keyword))

    if sort_field:
        order_by = ("" if sort_dir == "ASC" else "-") + sort_field
        maps = maps.order_by(order_by)

    maps_list = []

    for map in maps.all()[start:start+limit]:
        try:
            owner_name = Contact.objects.get(user=map.owner).name
            if not owner_name:
                owner_name = map.owner.username
        except:
            if map.owner.first_name:
                owner_name = map.owner.first_name + " " + map.owner.last_name
            else:
                owner_name = map.owner.username

        mapdict = {
            'id' : map.id,
            'title' : map.title,
            'abstract' : map.abstract,
            'urlsuffix' : map.urlsuffix,
            'detail' : reverse('geonode.maps.views.view', args=(map.id,)),
            'owner' : owner_name,
            'owner_detail' : reverse('profiles.views.profile_detail', args=(map.owner.username,)),
            'last_modified' : map.last_modified.isoformat()
            }
        maps_list.append(mapdict)

    result = {'rows': maps_list, 
              'total': maps.count()}

    result['query_info'] = {
        'start': start,
        'limit': limit,
        'q': query
    }
    if start > 0: 
        prev = max(start - limit, 0)
        params = urlencode({'q': query, 'start': prev, 'limit': limit})
        result['prev'] = reverse('geonode.maps.views.maps_search') + '?' + params

    next = start + limit + 1
    if next < maps.count():
         params = urlencode({'q': query, 'start': next - 1, 'limit': limit})
         result['next'] = reverse('geonode.maps.views.maps_search') + '?' + params
    
    return result

@csrf_exempt  
def searchFieldsJSON(request):
    logger.debug("Enter searchFieldsJSON")
    layername = request.POST.get('layername', False);
    logger.debug("layername is [%s]", layername)
    searchable_fields = []
    scount = 0
    editable = False
    catname = '';
    if layername:
        try:
            geoLayer = Layer.objects.get(typename=layername)

            category =geoLayer.topic_category
            if category is not None:
                catname = category.name
            else:
                catname = ''  
            if geoLayer.storeType == 'dataStore':
                #searchable_fields = geoLayer.searchable_fields
                #logger.debug('There are [%s] attributes', geoLayer.layerattribute_set.length)
                for la in geoLayer.layerattribute_set.filter(attribute__iregex=r'^((?!geom)(?!gid)(?!oid)(?!object[\w]*id).)*$'):
                    searchable_fields.append( {"attribute": la.attribute, "label": la.attribute_label, "searchable": str(la.searchable)})
                    if la.searchable:
                        scount+=1            
        except Exception, e: 
            logger.debug("Could not find matching layer: [%s]", str(e))            
        sfJSON = {'searchFields' : searchable_fields, 'category' : catname, 'scount' : scount}
        logger.debug('sfJSON is [%s]', str(sfJSON))
        return HttpResponse(json.dumps(sfJSON))
    else:
        logger.debug("searchFieldsJSON DID NOT WORK")
        

@csrf_exempt    
def maps_search_page(request):
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    return render_to_response('maps_search.html', RequestContext(request, {
        'init_search': json.dumps(params or {}),
         "site" : settings.SITEURL
    }))
    
@csrf_exempt
def ajax_url_lookup(request):
    if request.method != 'POST':
        return HttpResponse(
            content='ajax user lookup requires HTTP POST',
            status=405,
            mimetype='text/plain'
        )
    elif 'query' not in request.POST:
        return HttpResponse(
            content='use a field named "query" to specify a prefix to filter urls',
            mimetype='text/plain'
        )
    if request.POST['query'] != '':
        maps = Map.objects.filter(urlsuffix__startswith=request.POST['query'])
        if request.POST['mapid'] != '':
            maps = maps.exclude(id=request.POST['mapid'])
        json_dict = {
                         'urls': [({'url': m.urlsuffix}) for m in maps],
                         'count': maps.count(),
                    }
    else:
            json_dict = {
                            'urls' : [],
                             'count' : 0,
                         }
    return HttpResponse(
                            content=json.dumps(json_dict),
                            mimetype='text/plain'
    )

def updatelayers(request):
    try:
        Layer.objects.slurp()
    except:
        return HttpResponse(content=str(_), mimetype='text/plain')
    return HttpResponse('Layers successfully slurped', mimetype='text/plain')

def cleardeadlayers(request):
        try:
            pre_delete.disconnect(delete_layer, sender=Layer)
            cat = Layer.objects.gs_catalog
            storenames = [s.name for s in cat.get_stores()]
            layernames = [l.name for l in cat.get_resources()]
            for l in Layer.objects.all():
                if l.store not in storenames or l.name not in layernames:
                    l.delete()
                    print l
        except:
            return HttpResponse( "Couldn't connect to GeoServer; is it running? Make sure the GEOSERVER_BASE_URL setting is set correctly.", mimetype="text/plain")
        finally:
            pre_delete.connect(delete_layer, sender=Layer)
            return HttpResponse("Done clearing dead layers", mimetype='text/plain')

def upload_progress(request):
    """
    Return JSON object with information about the progress of an upload.
    """
    if 'HTTP_X_PROGRESS_ID' in request.META:
        progress_id = request.META['HTTP_X_PROGRESS_ID']
        from django.utils import simplejson
        cache_key = "%s_%s" % (request.META['REMOTE_ADDR'], progress_id)
        data = cache.get(cache_key)
        json = simplejson.dumps(data)
        return HttpResponse(json)
    else:
        logging.error("Received progress report request without X-Progress-ID header. request.META: %s" % request.META)
        return HttpResponseBadRequest('Server Error: You must provide X-Progress-ID header or query param.')
