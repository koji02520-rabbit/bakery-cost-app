// 画面の小さな補助。htmx より前に動くよう、キャプチャ段階で受け取る。

// 食材登録 STEP1：触った入力欄だけにエラーを出すため、欄の名前を _touched に記録する
function markTouched(event) {
  const field = event.target;
  const form = field.form;
  if (!form || !field.name || field.name.startsWith("_")) return;
  const touched = form.querySelector("input[name=_touched]");
  if (!touched) return;
  const names = new Set(touched.value.split(",").filter(Boolean));
  names.add(field.name);
  touched.value = [...names].join(",");
}
document.addEventListener("focusout", markTouched, true);
document.addEventListener("change", markTouched, true);

// 二重送信の防止：data-once を付けたフォームは、1回送信したらボタンを押せなくする
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!form.hasAttribute("data-once")) return;
  if (form.dataset.sent) {
    event.preventDefault();
    return;
  }
  form.dataset.sent = "1";
  // 押したボタンの name/value は送信に必要なので、無効化は送信処理のあとに行う
  setTimeout(() => form.querySelectorAll("button").forEach((b) => (b.disabled = true)), 0);
});

// ブラウザの「戻る」でページが復元されたときは、送信済みの印を消す
window.addEventListener("pageshow", () => {
  document.querySelectorAll("form[data-sent]").forEach((form) => {
    delete form.dataset.sent;
    form.querySelectorAll("button").forEach((b) => (b.disabled = false));
  });
});

// レシピ編集：材料検索パネルを閉じる（閉じるボタン・背景・Esc・追加完了）
function closePicker() {
  const picker = document.getElementById("picker");
  if (picker) picker.innerHTML = "";
}
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-close-picker]")) closePicker();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePicker();
});
// サーバーが HX-Trigger: picker-close を返したら閉じる（イベントは document まで伝わる）
document.addEventListener("picker-close", closePicker);

// レシピ編集：「≡」をつかんで並べ替え（スマートフォンは長押し）。並べ終えたら順番を保存する
function csrfHeaders() {
  try {
    return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
  } catch (e) {
    return {};
  }
}
function initSortable(root) {
  if (typeof Sortable === "undefined" || !root.querySelectorAll) return;
  root.querySelectorAll("[data-sortable]").forEach((list) => {
    if (list.dataset.sortableReady) return;
    list.dataset.sortableReady = "1";
    Sortable.create(list, {
      handle: ".handle",
      animation: 150,
      forceFallback: true, // ブラウザ標準のドラッグではなくマウス・指の動きで追う（スマートフォンとPCで同じ動き）
      delay: 250,
      delayOnTouchOnly: true,
      onEnd(evt) {
        if (evt.oldIndex === evt.newIndex) return;
        const order = [...list.querySelectorAll(":scope > [data-id]")].map((li) => li.dataset.id).join(",");
        htmx.ajax("POST", list.dataset.orderUrl, {
          target: "#editor-body", swap: "outerHTML", values: { order }, headers: csrfHeaders(),
        });
      },
    });
  });
}
document.addEventListener("htmx:load", (event) => initSortable(event.target));

// 栄養成分の入力：成分表の検索結果を押すと、その食品の100gあたりの値を入力欄に入れる
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-food]");
  const form = document.getElementById("nutrition-form");
  if (!button || !form) return;
  ["energy_kcal", "protein_g", "fat_g", "carbohydrate_g", "salt_g"].forEach((name) => {
    form.elements[name].value = button.dataset[name] || "";
  });
  form.elements.nutrition_food.value = button.dataset.food;
  form.elements.sodium_mg.value = "";
  if (!form.elements.nutrition_note.value) form.elements.nutrition_note.value = "日本食品標準成分表（八訂）";
  document.getElementById("picked-food").textContent = "成分表：" + button.dataset.label + "（値を入れました。必要なら直して保存してください）";
  form.scrollIntoView({ behavior: "smooth", block: "start" });
});

// レシピの写真：選んだ写真を送る前に縮める（長い辺 1600px の JPEG）。スマートフォンの通信量と待ち時間を減らす。
// 縮められない形式（ブラウザが読めない画像）はそのまま送り、サーバー側で判定する。
const PHOTO_MAX = 1600;
async function shrinkPhoto(file) {
  if (!file.type.startsWith("image/") || typeof createImageBitmap !== "function") return file;
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, PHOTO_MAX / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch (e) {
    return file;
  }
}
document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!input.matches || !input.matches("[data-photo-input]") || !input.files.length) return;
  const form = input.form;
  const indicator = document.querySelector(form.getAttribute("hx-indicator")) || form;
  indicator.classList.add("htmx-request"); // 縮めている間も「送っています」を出す
  try {
    const shrunk = await Promise.all([...input.files].map(shrinkPhoto));
    const transfer = new DataTransfer();
    shrunk.forEach((f) => transfer.items.add(f));
    input.files = transfer.files;
  } catch (e) {
    // DataTransfer が使えない古いブラウザでは、元の写真をそのまま送る
  }
  indicator.classList.remove("htmx-request");
  form.requestSubmit();
});
window.addEventListener("load", () => initSortable(document));
