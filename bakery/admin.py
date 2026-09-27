"""Django管理画面（S50）。ユーザー・店舗・カテゴリー・アレルゲン項目・税率などの設定に使う。

食材・レシピ・商品の登録や価格変更は、影響確認や履歴記録のために専用画面から行う。
管理画面では閲覧のみにして、確認を通らない変更を防ぐ。
"""

from django.contrib import admin

from bakery import models


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name"]


@admin.register(models.Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "status"]


@admin.register(models.Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "company", "store", "role"]
    list_filter = ["role"]


@admin.register(models.SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ["company", "tax_rate", "unit_price_rounding", "default_yield_rate"]


@admin.register(models.IngredientCategory)
class IngredientCategoryAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "sort_order", "status"]
    list_editable = ["sort_order", "status"]
    exclude = ["created_by", "updated_by"]


@admin.register(models.AllergenItem)
class AllergenItemAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "sort_order", "status"]
    list_filter = ["kind"]
    list_editable = ["sort_order", "status"]
    exclude = ["created_by", "updated_by"]


@admin.register(models.Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "contact_person", "phone", "status"]
    readonly_fields = ["code"]
    exclude = ["created_by", "updated_by"]

    def save_model(self, request, obj, form, change):
        if not obj.code:
            obj.code = models.IdSequence.issue(obj.company, "SUP")
        super().save_model(request, obj, form, change)


@admin.register(models.Ingredient)
class IngredientAdmin(ReadOnlyAdmin):
    list_display = ["code", "name", "category", "supplier", "purchase_weight_g", "purchase_price", "status"]
    list_filter = ["category", "status", "allergen_status"]
    search_fields = ["code", "name", "legacy_code"]


@admin.register(models.Recipe)
class RecipeAdmin(ReadOnlyAdmin):
    list_display = ["code", "name", "version", "kind", "recipe_type", "status"]
    list_filter = ["kind", "recipe_type", "status"]
    search_fields = ["code", "name"]


@admin.register(models.Product)
class ProductAdmin(ReadOnlyAdmin):
    list_display = ["code", "name", "price_excluding_tax", "adopted_recipe", "status"]
    search_fields = ["code", "name"]


@admin.register(models.AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ["at", "user", "action", "target_type", "target_label"]
    list_filter = ["action", "target_type"]
    search_fields = ["target_label"]
