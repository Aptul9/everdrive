from django.urls import path, re_path
from storage import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("anonymous/", views.anonymous, name="anonymous"),
    path("healthz/", views.health, name="health"),
    path("api/storage/", views.capacity, name="capacity"),
    path("api/files/", views.files, name="files"),
    path("api/anonymous-files/", views.anonymous_files, name="anonymous-files"),
    path("api/uploads/", views.begin, name="begin"),
    path("api/uploads/<uuid:file_id>/chunk/", views.chunk, name="chunk"),
    path("api/uploads/<uuid:file_id>/finish/", views.finish, name="finish"),
    path("api/uploads/<uuid:file_id>/cancel/", views.cancel, name="cancel"),
    path("api/files/<uuid:file_id>/download/", views.private_download, name="private-download"),
    path("api/files/<uuid:file_id>/delete/", views.delete, name="delete"),
    re_path(r"^s/(?P<token>[A-Za-z0-9_-]{43})/$", views.share, name="share"),
    re_path(r"^s/(?P<token>[A-Za-z0-9_-]{43})/download/$", views.public_download, name="public-download"),
]
