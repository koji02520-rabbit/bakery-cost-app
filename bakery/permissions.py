"""ログイン・所属・権限のチェック（spec.md 第4項）。

画面のビューは member_required か admin_required を付ける。どちらも request.membership と
request.company を用意するので、ビューの中では必ず request.company で会社を絞り込む。
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from bakery.models import Membership


def _attach_membership(request):
    try:
        membership = request.user.membership
    except Membership.DoesNotExist:
        return False
    request.membership = membership
    request.company = membership.company
    return True


def member_required(view):
    """ログイン済みで、どこかの会社に所属しているユーザーだけが使える画面。"""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not _attach_membership(request):
            return render(request, "bakery/no_membership.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    """管理者だけが使える画面（食材マスタ管理・仕入先マスタ管理・使用停止・削除など）。"""

    @member_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.membership.is_admin:
            raise PermissionDenied("この操作は管理者のみ行えます")
        return view(request, *args, **kwargs)

    return wrapper
