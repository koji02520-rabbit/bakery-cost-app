"""画面の入力フォーム。エラーは「何を直せばよいか分かる日本語」で出す（spec.md 第14項）。"""

import unicodedata
from decimal import Decimal

from django import forms
from django.db.models import Q

from bakery.models import (
    AllergenItem,
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeKind,
    RecipeType,
    Status,
    Supplier,
)


class JapaneseDecimalField(forms.DecimalField):
    """全角数字・カンマ・前後の空白を許して読み取る数値欄（スマートフォンの日本語入力対策）。"""

    def __init__(self, *, label_for_message, unit="", min_value=None, **kwargs):
        self.label_for_message = label_for_message
        messages = {
            "required": f"{label_for_message}を入力してください",
            "invalid": f"{label_for_message}は数字で入力してください",
            "max_digits": f"{label_for_message}の桁数が多すぎます",
            "max_decimal_places": f"{label_for_message}の小数は第{kwargs.get('decimal_places', 2)}位までにしてください",
            "max_whole_digits": f"{label_for_message}の桁数が多すぎます",
        }
        if min_value is not None:
            messages["min_value"] = f"{label_for_message}は{min_value}{unit}以上で入力してください"
        super().__init__(min_value=min_value, error_messages=messages, localize=False, **kwargs)
        self.widget = forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"})

    def to_python(self, value):
        if isinstance(value, str):
            value = unicodedata.normalize("NFKC", value).replace(",", "").strip()
        return super().to_python(value)


class IngredientStep1Form(forms.Form):
    """食材登録 STEP1（S11）。入力順：食材名→カテゴリー→仕入先→購入重量→購入価格（第12項）。"""

    name = forms.CharField(
        label="食材名", max_length=100,
        error_messages={"required": "食材名を入力してください",
                        "max_length": "食材名は100文字以内で入力してください"},
    )
    category = forms.ModelChoiceField(
        label="カテゴリー", queryset=IngredientCategory.objects.none(), empty_label="選んでください",
        error_messages={"required": "カテゴリーを選択してください",
                        "invalid_choice": "カテゴリーを選択し直してください"},
    )
    supplier = forms.ModelChoiceField(
        label="仕入先", queryset=Supplier.objects.none(), empty_label="選んでください",
        error_messages={"required": "仕入先を選択してください",
                        "invalid_choice": "仕入先を選択し直してください"},
    )
    purchase_weight_g = JapaneseDecimalField(
        label="購入重量", label_for_message="購入重量", unit="g", min_value=Decimal("1"),
        max_digits=12, decimal_places=2,
    )
    purchase_price = JapaneseDecimalField(
        label="購入価格", label_for_message="購入価格", unit="円", min_value=Decimal("0"),
        max_digits=12, decimal_places=2,
    )

    FIELD_ORDER = ["name", "category", "supplier", "purchase_weight_g", "purchase_price"]

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["category"].queryset = IngredientCategory.objects.filter(company=company, status=Status.ACTIVE)
        self.fields["supplier"].queryset = Supplier.objects.filter(company=company, status=Status.ACTIVE)
        self.fields["name"].widget.attrs.update({"autocomplete": "off"})

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("食材名を入力してください")
        return name

    def duplicates(self):
        """同じ食材名・同じ仕入先の食材（確認画面で知らせる。仕入先が違えば別食材として扱う：第18項）。"""
        data = self.cleaned_data
        return Ingredient.objects.filter(company=self.company, name=data["name"], supplier=data["supplier"])


class IngredientEditForm(forms.ModelForm):
    """食材の基本情報の修正。購入重量・価格は「価格を変更する」から行う（影響確認と履歴のため）。

    カテゴリーを変えても食材IDは変わらない（第8項）。
    """

    class Meta:
        model = Ingredient
        fields = ["name", "category", "supplier", "note"]
        error_messages = {
            "name": {"required": "食材名を入力してください"},
            "category": {"required": "カテゴリーを選択してください"},
            "supplier": {"required": "仕入先を選択してください"},
        }
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        # 使用停止中のカテゴリー・仕入先でも、今設定されているものは選べるままにする
        self.fields["category"].queryset = IngredientCategory.objects.filter(
            Q(status=Status.ACTIVE) | Q(pk=instance.category_id), company=company
        )
        self.fields["supplier"].queryset = Supplier.objects.filter(
            Q(status=Status.ACTIVE) | Q(pk=instance.supplier_id), company=company
        )
        self.fields["category"].empty_label = self.fields["supplier"].empty_label = "選んでください"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("食材名を入力してください")
        return name


class SupplierQuickForm(forms.Form):
    """食材登録の途中で仕入先名だけを登録する（S11 の注記、第61項）。"""

    supplier_name = forms.CharField(
        label="新しい仕入先名", max_length=100,
        error_messages={"required": "仕入先名を入力してください",
                        "max_length": "仕入先名は100文字以内で入力してください"},
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_supplier_name(self):
        name = self.cleaned_data["supplier_name"].strip()
        if not name:
            raise forms.ValidationError("仕入先名を入力してください")
        if Supplier.objects.filter(company=self.company, name=name).exists():
            raise forms.ValidationError(f"仕入先「{name}」はすでに登録されています。一覧から選んでください")
        return name


class AllergenForm(forms.Form):
    """アレルゲン入力（S13）。義務を上、推奨を下に並べる。"""

    allergens = forms.ModelMultipleChoiceField(
        queryset=AllergenItem.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allergens"].queryset = AllergenItem.objects.filter(company=company, status=Status.ACTIVE)

    def split_by_kind(self):
        """テンプレート用：(義務の選択肢, 推奨の選択肢)。"""
        selected = {str(v) for v in (self["allergens"].value() or [])}
        mandatory, recommended = [], []
        for item in self.fields["allergens"].queryset:
            entry = {"item": item, "checked": str(item.pk) in selected}
            (mandatory if item.is_mandatory else recommended).append(entry)
        return mandatory, recommended



# --------------------------------------------------------------------------
# レシピ
# --------------------------------------------------------------------------


class RecipeCreateForm(forms.Form):
    """レシピを作る（S21）：新規／コピー → 区分 → 種類 → レシピ名。"""

    mode = forms.ChoiceField(
        choices=[("new", "新しく作る"), ("copy", "コピーして作る")], initial="new", widget=forms.RadioSelect,
        error_messages={"required": "作り方を選んでください"},
    )
    source = forms.ModelChoiceField(
        queryset=Recipe.objects.none(), required=False, empty_label="コピー元のレシピを選んでください",
        error_messages={"invalid_choice": "コピー元のレシピを選び直してください"},
    )
    kind = forms.ChoiceField(
        choices=RecipeKind.choices, required=False, widget=forms.RadioSelect,
    )
    recipe_type = forms.ChoiceField(
        choices=[("", "選んでください")] + list(RecipeType.choices), required=False,
    )
    name = forms.CharField(
        max_length=100, error_messages={"required": "レシピ名を入力してください",
                                        "max_length": "レシピ名は100文字以内で入力してください"},
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = Recipe.objects.filter(company=company).order_by("kind", "name", "-version")
        self.fields["name"].widget.attrs.update({"autocomplete": "off", "placeholder": "例：クロワッサン生地"})

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("レシピ名を入力してください")
        return name

    def clean(self):
        data = super().clean()
        if data.get("mode") == "copy":
            if not data.get("source"):
                self.add_error("source", "コピー元のレシピを選んでください")
        else:
            kind = data.get("kind")
            if not kind:
                self.add_error("kind", "レシピ区分（中間レシピ／最終商品レシピ）を選んでください")
            elif kind == RecipeKind.INTERMEDIATE and not data.get("recipe_type"):
                self.add_error("recipe_type", "中間レシピの種類を選んでください")
        return data


class RecipeInfoForm(forms.ModelForm):
    """レシピ名・種類・下処理・手順・メモ。原価に影響しないので使用中でも修正できる（第30項 v16）。

    種類は使用中でなければ変えられる。
    """

    class Meta:
        model = Recipe
        fields = ["name", "recipe_type", "preparation", "procedure", "memo"]
        error_messages = {"name": {"required": "レシピ名を入力してください"}}
        widgets = {
            "preparation": forms.Textarea(attrs={"rows": 3}),
            "procedure": forms.Textarea(attrs={"rows": 6}),
            "memo": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, lock_type=False, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.kind != RecipeKind.INTERMEDIATE or lock_type:
            del self.fields["recipe_type"]
        else:
            self.fields["recipe_type"].required = True
            self.fields["recipe_type"].choices = list(RecipeType.choices)
            self.fields["recipe_type"].error_messages["required"] = "中間レシピの種類を選んでください"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("レシピ名を入力してください")
        return name


class RecipeItemForm(forms.Form):
    """材料1行の使用量と工程。"""

    quantity_g = JapaneseDecimalField(
        label="使用量", label_for_message="使用量", unit="g", min_value=Decimal("0.01"),
        max_digits=10, decimal_places=2,
    )
    step_label = forms.CharField(
        label="工程", max_length=50, required=False,
        error_messages={"max_length": "工程は50文字以内で入力してください"},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity_g"].error_messages["min_value"] = "使用量は0gより大きい数で入力してください"
        self.fields["step_label"].widget.attrs.update({"placeholder": "例：1、ミキシング", "autocomplete": "off"})

    def clean_step_label(self):
        return self.cleaned_data["step_label"].strip()

    def first_error(self):
        for errors in self.errors.values():
            return errors[0]
        return ""


# --------------------------------------------------------------------------
# 商品
# --------------------------------------------------------------------------


class PriceChangeForm(forms.Form):
    """食材価格の変更（S41）。購入重量も変えられる（袋の大きさが変わった場合など）。"""

    purchase_weight_g = JapaneseDecimalField(
        label="購入重量", label_for_message="購入重量", unit="g", min_value=Decimal("1"),
        max_digits=12, decimal_places=2,
    )
    purchase_price = JapaneseDecimalField(
        label="購入価格", label_for_message="購入価格", unit="円", min_value=Decimal("0"),
        max_digits=12, decimal_places=2,
    )


class NutritionForm(forms.Form):
    """食材の栄養成分（可食部100gあたり、v26）。5項目すべてを入れると「入力済み」になる。

    食塩相当量が分からずナトリウム(mg)だけ分かる場合は、ナトリウムから換算する。
    """

    energy_kcal = JapaneseDecimalField(label="熱量", label_for_message="熱量", unit="kcal",
                                       min_value=Decimal("0"), max_digits=8, decimal_places=2, required=False)
    protein_g = JapaneseDecimalField(label="たんぱく質", label_for_message="たんぱく質", unit="g",
                                     min_value=Decimal("0"), max_digits=8, decimal_places=2, required=False)
    fat_g = JapaneseDecimalField(label="脂質", label_for_message="脂質", unit="g",
                                 min_value=Decimal("0"), max_digits=8, decimal_places=2, required=False)
    carbohydrate_g = JapaneseDecimalField(label="炭水化物", label_for_message="炭水化物", unit="g",
                                          min_value=Decimal("0"), max_digits=8, decimal_places=2, required=False)
    salt_g = JapaneseDecimalField(label="食塩相当量", label_for_message="食塩相当量", unit="g",
                                  min_value=Decimal("0"), max_digits=8, decimal_places=2, required=False)
    sodium_mg = JapaneseDecimalField(label="ナトリウム", label_for_message="ナトリウム", unit="mg",
                                     min_value=Decimal("0"), max_digits=10, decimal_places=2, required=False)
    nutrition_food = forms.ModelChoiceField(queryset=None, required=False, widget=forms.HiddenInput)
    nutrition_note = forms.CharField(label="栄養値の出どころ", max_length=200, required=False)

    UNITS = {"energy_kcal": "kcal", "protein_g": "g", "fat_g": "g", "carbohydrate_g": "g", "salt_g": "g"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from bakery.models import FoodComposition

        self.fields["nutrition_food"].queryset = FoodComposition.objects.all()
        self.fields["nutrition_note"].widget.attrs.update(
            {"placeholder": "例：商品ラベル、仕入先の規格書", "autocomplete": "off"})

    def clean(self):
        from bakery.services.nutrition import salt_from_sodium_mg

        data = super().clean()
        if data.get("salt_g") is None and data.get("sodium_mg") is not None:
            data["salt_g"] = salt_from_sodium_mg(data["sodium_mg"])
        filled = [f for f in self.UNITS if data.get(f) is not None]
        if filled and len(filled) < len(self.UNITS):
            missing = "・".join(self.fields[f].label for f in self.UNITS if data.get(f) is None)
            raise forms.ValidationError(
                f"{missing}も入力してください（含まれない場合は 0 を入力します）。5項目そろうと計算に使われます")
        return data

    def nutrition_fields(self):
        return [(self[f], self.UNITS[f]) for f in self.UNITS]


class JapaneseIntegerField(forms.IntegerField):
    """全角数字・カンマ・「円」を許して読み取る整数欄。"""

    def __init__(self, *, label_for_message, **kwargs):
        messages = {
            "required": f"{label_for_message}を入力してください",
            "invalid": f"{label_for_message}は整数（円）で入力してください",
            "min_value": f"{label_for_message}は0円以上で入力してください",
            "max_value": f"{label_for_message}が大きすぎます",
        }
        super().__init__(min_value=0, max_value=9_999_999, error_messages=messages, **kwargs)
        self.widget = forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"})

    def to_python(self, value):
        if isinstance(value, str):
            value = unicodedata.normalize("NFKC", value).replace(",", "").replace("円", "").strip()
        return super().to_python(value)


class ProductForm(forms.Form):
    """商品登録・修正（S32）。採用レシピは登録時だけここで選ぶ。登録後の変更は切替確認（S33）を通す。"""

    name = forms.CharField(
        label="商品名", max_length=100,
        error_messages={"required": "商品名を入力してください", "max_length": "商品名は100文字以内で入力してください"},
    )
    price_excluding_tax = JapaneseIntegerField(label="税抜売価", label_for_message="税抜売価")
    adopted_recipe = forms.ModelChoiceField(
        label="採用レシピ（最終商品レシピ）", queryset=Recipe.objects.none(), required=False,
        empty_label="あとで設定する",
        error_messages={"invalid_choice": "採用レシピを選び直してください"},
    )

    def __init__(self, *args, company, with_recipe=True, **kwargs):
        super().__init__(*args, **kwargs)
        from bakery.services.products import adoptable_recipes

        if with_recipe:
            self.fields["adopted_recipe"].queryset = adoptable_recipes(company)
        else:
            del self.fields["adopted_recipe"]
        self.fields["name"].widget.attrs.update({"autocomplete": "off", "placeholder": "例：クロワッサン"})
        self.fields["price_excluding_tax"].widget.attrs.update({"placeholder": "例：280"})

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("商品名を入力してください")
        return name
