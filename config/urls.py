from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = admin.site.site_title = "原価管理 管理画面"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
    path("", include("bakery.urls")),
]
