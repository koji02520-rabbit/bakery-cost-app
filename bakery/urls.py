from django.urls import path

from bakery.views import common, ingredients, photos, pricing, products, recipes

urlpatterns = [
    path("", common.home, name="home"),

    # 食材（S10〜S14）
    path("ingredients/", ingredients.ingredient_list, name="ingredient_list"),
    path("ingredients/new/", ingredients.ingredient_new, name="ingredient_new"),
    path("ingredients/new/check/", ingredients.ingredient_new_check, name="ingredient_new_check"),
    path("ingredients/<int:pk>/", ingredients.ingredient_detail, name="ingredient_detail"),
    path("ingredients/<int:pk>/registered/", ingredients.ingredient_registered, name="ingredient_registered"),
    path("ingredients/<int:pk>/allergens/", ingredients.ingredient_allergens, name="ingredient_allergens"),
    path("ingredients/<int:pk>/edit/", ingredients.ingredient_edit, name="ingredient_edit"),
    path("ingredients/<int:pk>/status/", ingredients.ingredient_set_status, name="ingredient_set_status"),
    path("ingredients/<int:pk>/delete/", ingredients.ingredient_delete, name="ingredient_delete"),
    path("suppliers/quick-add/", ingredients.supplier_quick_add, name="supplier_quick_add"),
    path("ingredients/<int:pk>/nutrition/", ingredients.ingredient_nutrition, name="ingredient_nutrition"),
    path("food-composition/search/", ingredients.food_search, name="food_search"),

    # レシピ（S20〜S25）
    path("recipes/", recipes.recipe_list, name="recipe_list"),
    path("recipes/new/", recipes.recipe_new, name="recipe_new"),
    path("recipes/replace/", recipes.recipe_replace, name="recipe_replace"),
    path("recipes/<int:pk>/", recipes.recipe_detail, name="recipe_detail"),
    path("recipes/<int:pk>/edit/", recipes.recipe_edit, name="recipe_edit"),
    path("recipes/<int:pk>/info/", recipes.recipe_info_edit, name="recipe_info_edit"),
    path("recipes/<int:pk>/new-version/", recipes.recipe_new_version, name="recipe_new_version"),
    path("recipes/<int:pk>/picker/", recipes.recipe_picker, name="recipe_picker"),
    path("recipes/<int:pk>/items/add/", recipes.recipe_item_add, name="recipe_item_add"),
    path("recipes/<int:pk>/items/order/", recipes.recipe_reorder, name="recipe_reorder"),
    path("recipes/<int:pk>/status/", recipes.recipe_set_status, name="recipe_set_status"),
    path("recipes/<int:pk>/delete/", recipes.recipe_delete, name="recipe_delete"),
    path("recipe-items/<int:pk>/update/", recipes.recipe_item_update, name="recipe_item_update"),
    path("recipe-items/<int:pk>/material/", recipes.recipe_item_material, name="recipe_item_material"),
    path("recipe-items/<int:pk>/delete/", recipes.recipe_item_delete, name="recipe_item_delete"),

    # 商品（S30〜S33）
    path("products/", products.product_list, name="product_list"),
    path("products/export/<str:fmt>/", products.product_export, name="product_export"),
    path("products/new/", products.product_new, name="product_new"),
    path("products/<int:pk>/", products.product_detail, name="product_detail"),
    path("products/<int:pk>/edit/", products.product_edit, name="product_edit"),
    path("products/<int:pk>/switch/", products.product_switch, name="product_switch"),
    path("products/<int:pk>/status/", products.product_set_status, name="product_set_status"),

    # 写真（レシピ・商品）
    path("products/<int:pk>/photos/", photos.product_photos, name="product_photos"),
    path("recipes/<int:pk>/photos/add/", photos.photos_add, {"kind": "recipe"}, name="recipe_photos_add"),
    path("recipe-photos/<int:pk>/<str:size>.jpg", photos.photo_file, {"kind": "recipe"}, name="recipe_photo_file"),
    path("recipe-photos/<int:pk>/caption/", photos.photo_caption, {"kind": "recipe"}, name="recipe_photo_caption"),
    path("recipe-photos/<int:pk>/move/", photos.photo_move, {"kind": "recipe"}, name="recipe_photo_move"),
    path("recipe-photos/<int:pk>/delete/", photos.photo_delete, {"kind": "recipe"}, name="recipe_photo_delete"),
    path("products/<int:pk>/photos/add/", photos.photos_add, {"kind": "product"}, name="product_photos_add"),
    path("product-photos/<int:pk>/<str:size>.jpg", photos.photo_file, {"kind": "product"}, name="product_photo_file"),
    path("product-photos/<int:pk>/caption/", photos.photo_caption, {"kind": "product"}, name="product_photo_caption"),
    path("product-photos/<int:pk>/move/", photos.photo_move, {"kind": "product"}, name="product_photo_move"),
    path("product-photos/<int:pk>/delete/", photos.photo_delete, {"kind": "product"}, name="product_photo_delete"),

    # 価格変更（S40〜S42）
    path("price-change/", pricing.price_change_select, name="price_change"),
    path("price-change/<int:pk>/", pricing.price_change_edit, name="price_change_edit"),
    path("price-change/<int:pk>/check/", pricing.price_change_check, name="price_change_check"),
]
