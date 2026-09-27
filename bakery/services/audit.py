"""変更履歴（AuditLog）の記録。誰が・いつ・何を・変更前・変更後（spec.md 第52項）。"""

from decimal import Decimal

from django.db import models

from bakery.models import AuditLog


def snapshot(obj, fields=None):
    """モデルの値を JSON に保存できる dict にする。外部キーは内部IDと表示名の両方を残す。"""
    data = {}
    for field in obj._meta.concrete_fields:
        if fields is not None and field.name not in fields:
            continue
        if field.name in ("created_at", "updated_at", "created_by", "updated_by", "company"):
            continue
        value = getattr(obj, field.attname)
        if isinstance(field, models.ForeignKey):
            related = getattr(obj, field.name)
            data[field.name] = {"id": value, "label": str(related)} if related else None
        elif isinstance(value, Decimal):
            data[field.name] = str(value)
        elif hasattr(value, "isoformat"):
            data[field.name] = value.isoformat()
        else:
            data[field.name] = value
    for field in obj._meta.many_to_many:
        if fields is not None and field.name not in fields:
            continue
        if obj.pk:
            data[field.name] = sorted(str(o) for o in getattr(obj, field.name).all())
    return data


def record(request, action, obj, before=None, after=None):
    return AuditLog.objects.create(
        company=request.company,
        user=request.user,
        action=action,
        target_type=obj._meta.model_name,
        target_id=obj.pk,
        target_label=str(obj)[:200],
        before=before,
        after=after,
    )
