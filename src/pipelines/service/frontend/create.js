import "./app.js";
import { editAdImages, generateAd, retryDelay } from "./api.js";
import { listChats, loadChat, saveChat } from "./chats.js";
import { getPublishDraft, restorePublishDraft, setApiResults } from "./publish.js?v=export-selection-count";
import { describeResultImages } from "./result-images.js";

const MAX_PHOTO_BYTES = 10 * 1024 * 1024;
const MAX_PROMPT_LENGTH = 200;
const PHOTO_TYPES = ["image/jpeg", "image/png"];
const previewFirstResult = new URLSearchParams(location.search).get("preview") === "first-result";

const form = document.querySelector("#create-form");
const photoInput = document.querySelector("#photo");
const photoDrop = document.querySelector("#photo-drop");
const photoPreview = document.querySelector("#photo-preview");
const photoImage = document.querySelector("#photo-image");
const photoName = document.querySelector("#photo-name");
const photoError = document.querySelector("#photo-error");
const prompt = document.querySelector("#prompt");
const promptCount = document.querySelector("#prompt-count");

let selectedPhoto = null;
let savedPhoto = null;
let previewUrl = "";

// 첨부 파일의 개수와 형식, 크기를 검사해 오류 문구를 반환합니다.
function photoValidationMessage(files) {
  if (files.length !== 1) return "사진을 한 장만 첨부해 주세요.";
  const [file] = files;
  if (!PHOTO_TYPES.includes(file.type)) return "JPG 또는 PNG 사진을 선택해 주세요.";
  if (file.size > MAX_PHOTO_BYTES) return "10 MiB 이하의 사진을 선택해 주세요.";
  return "";
}

// 첨부 오류 문구와 입력창의 접근성 상태를 갱신합니다.
function showPhotoError(message) {
  photoError.textContent = message;
  photoInput.setAttribute("aria-invalid", String(Boolean(message)));
}

// 첨부 사진과 미리보기 주소를 정리하고 화면에서 숨깁니다.
function clearPhoto() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = "";
  selectedPhoto = null;
  photoImage.removeAttribute("src");
  photoName.textContent = "";
  photoPreview.hidden = true;
}

// 검증한 사진의 미리보기를 표시하고 새 첨부만 저장하도록 예약합니다.
function selectPhoto(files, { restored = false } = {}) {
  const message = photoValidationMessage(files);
  showPhotoError(message);
  if (message) return;

  clearPhoto();
  [selectedPhoto] = files;
  previewUrl = URL.createObjectURL(selectedPhoto);
  photoImage.src = previewUrl;
  photoName.textContent = selectedPhoto.name;
  photoPreview.hidden = false;
  if (restored) savedPhoto = selectedPhoto;
  else scheduleSave();
}

photoInput.addEventListener("change", () => {
  if (photoInput.files.length) selectPhoto(photoInput.files);
  photoInput.value = "";
});

photoDrop.addEventListener("dragover", (event) => {
  event.preventDefault();
  photoDrop.classList.add("is-dragging");
});
photoDrop.addEventListener("dragleave", (event) => {
  if (!photoDrop.contains(event.relatedTarget)) photoDrop.classList.remove("is-dragging");
});
photoDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  photoDrop.classList.remove("is-dragging");
  selectPhoto(event.dataTransfer.files);
});

document.querySelector("#remove-photo").addEventListener("click", () => {
  clearPhoto();
  showPhotoError("");
  photoInput.focus();
  scheduleSave();
});

photoImage.addEventListener("error", () => {
  if (!previewUrl) return;
  clearPhoto();
  showPhotoError("사진을 열 수 없습니다. 다른 사진을 선택해 주세요.");
});

// 프롬프트 글자 수를 표시하고 입력 제한을 적용합니다.
function validatePrompt(field, counter) {
  const length = Array.from(field.value.trim()).length;
  counter.textContent = `${length} / ${MAX_PROMPT_LENGTH}자`;
  counter.classList.toggle("field-error", length > MAX_PROMPT_LENGTH);
  if (!length && field.required) field.setCustomValidity("프롬프트를 입력해 주세요.");
  else if (length > MAX_PROMPT_LENGTH) field.setCustomValidity("프롬프트는 200자까지 입력할 수 있습니다.");
  else field.setCustomValidity("");
}

// 매장 정보 입력값을 확인하고 각 필드의 오류 상태를 갱신합니다.
function validateShopFields() {
  for (const name of ["shop", "instagramId", "businessHours", "booking"]) {
    const field = form.elements.namedItem(name);
    field.value = field.value.trim();
    field.setCustomValidity("");
  }

  const account = form.elements.instagramId;
  if (account.value.startsWith("@")) account.setCustomValidity("@를 빼고 계정명을 입력해 주세요.");

  const booking = form.elements.booking;
  if (booking.value && !/^https?:\/\//i.test(booking.value)) {
    booking.setCustomValidity("http:// 또는 https://로 시작하는 예약 링크를 입력해 주세요.");
  }
}

form.addEventListener("input", (event) => {
  if (event.target === prompt) validatePrompt(prompt, promptCount);
  else event.target.setCustomValidity("");
  scheduleSave();
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  showPhotoError(selectedPhoto ? "" : "네일 사진을 첨부해 주세요.");
  if (!selectedPhoto) {
    photoInput.focus();
    return;
  }
  validateShopFields();
  validatePrompt(prompt, promptCount);
  if (form.reportValidity()) generateImages();
});

const createTitle = document.querySelector("#create-title");
const resultPanel = document.querySelector("#example-results");
const commonPrompt = document.querySelector("#common-edit-prompt");
const commonPromptCount = document.querySelector("#common-edit-count");
const editDialog = document.querySelector("#image-edit-dialog");
const editForm = document.querySelector("#image-edit-form");
const imagePrompt = document.querySelector("#image-edit-prompt");
const imagePromptCount = document.querySelector("#image-edit-count");
const selectedImagePreview = document.querySelector("#selected-image-preview");
const selectedImageName = document.querySelector("#selected-image-name");
let activeImageIndex;
let imagePrompts = [];
let apiResults;
const resultGrid = document.querySelector(".result-grid");

// 개별 이미지 수정 지시를 공통 입력 영역에서 확인할 수 있게 표시합니다.
function renderImagePromptSummary(images = describeResultImages(apiResults)) {
  const items = images.flatMap((image, index) => {
    const prompt = imagePrompts[index];
    if (!prompt) return [];
    const item = document.createElement("li");
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = image.title;
    text.append(title, ` - ${prompt}`);
    const remove = document.createElement("button");
    remove.className = "image-prompt-remove";
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `${image.title} 수정 지시 삭제`);
    remove.addEventListener("click", () => {
      imagePrompts[index] = "";
      failedEdits = [];
      renderImagePromptSummary(images);
      commonPrompt.focus();
      scheduleSave();
    });
    item.append(text, remove);
    return [item];
  });
  document.querySelector("#image-prompt-summary").replaceChildren(...items);
}

// 서버가 반환한 이미지와 해당 이미지의 수정 버튼을 카드로 만듭니다.
function createResultCard(image, index) {
  const card = document.createElement("figure");
  const visual = document.createElement("div");
  visual.className = `image-placeholder ${image.style}`;
  const picture = document.createElement("img");
  picture.src = image.image_url;
  picture.alt = image.title;
  if (image.image_url) visual.append(picture);
  else if (previewFirstResult) {
    visual.append(document.createElement("i"));
    visual.setAttribute("aria-hidden", "true");
  }
  const label = document.createElement("figcaption");
  label.textContent = image.title;
  card.append(visual, label);
  const editor = document.createElement("div");
  editor.className = "result-image-edit";
  editor.innerHTML = '<button class="button button-outline" type="button">이 이미지 수정</button>';
  editor.querySelector("button").dataset.imageEdit = index;
  card.append(editor);
  return card;
}

// 결과 카드와 내보내기 목록을 갱신하고 결과 화면을 엽니다.
function showResults() {
  const images = describeResultImages(apiResults);
  setApiResults(apiResults, images);
  resultGrid.replaceChildren(...images.map(createResultCard));
  renderImagePromptSummary(images);
  createTitle.hidden = true;
  form.hidden = true;
  resultPanel.hidden = false;
  resultPanel.focus();
}

commonPrompt.addEventListener("input", () => {
  validatePrompt(commonPrompt, commonPromptCount);
  failedEdits = [];
  scheduleSave();
});
imagePrompt.addEventListener("input", () => validatePrompt(imagePrompt, imagePromptCount));

resultGrid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-image-edit]");
  if (!button) return;
  const card = button.closest("figure");
  const visual = card.querySelector(".image-placeholder");
  activeImageIndex = Number(button.dataset.imageEdit);
  selectedImageName.textContent = card.querySelector("figcaption").textContent;
  selectedImagePreview.replaceChildren(...Array.from(visual.children, (child) => child.cloneNode(true)));
  selectedImagePreview.className = visual.className;
  imagePrompt.value = imagePrompts[activeImageIndex] ?? "";
  validatePrompt(imagePrompt, imagePromptCount);
  editDialog.showModal();
  imagePrompt.focus();
});

document.querySelector("#cancel-image-edit").addEventListener("click", () => editDialog.close());

const batchStatus = document.querySelector("#batch-edit-status");
const applyEdits = document.querySelector("#apply-edits");
let failedEdits = [];
let editing = false;
let editCooldownUntil = 0;

// 개별 수정 지시를 검증해 초안에 반영하고 저장을 예약합니다.
function saveImagePrompt() {
  if (!imagePrompt.reportValidity()) return false;
  imagePrompts[activeImageIndex] = imagePrompt.value.trim();
  failedEdits = [];
  renderImagePromptSummary();
  scheduleSave();
  return true;
}

// 공통 지시를 받을 네일팁과 개별 지시가 있는 이미지를 모읍니다.
function editTargets() {
  const common = commonPrompt.value.trim();
  return (apiResults?.results ?? []).flatMap((image, index) => {
    const image_prompt = imagePrompts[index] ?? "";
    if (!image_prompt && !(common && image.type === "nail_tip")) return [];
    return [{ index, image_id: image.image_id, image_prompt }];
  });
}

// 이미지 수정 진행 상태에 맞춰 조작 버튼과 입력창을 갱신합니다.
function showEditProgress(busy) {
  editing = busy;
  resultPanel.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  editForm.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  commonPrompt.readOnly = busy;
  imagePrompt.readOnly = busy;
}

// 이미지 수정을 요청하고 성공한 결과와 재시도 대상을 각각 반영합니다.
async function submitImageEdits(targets, common, status) {
  if (editing || Date.now() < editCooldownUntil) return false;
  if (!targets.length) {
    status.textContent = "공통 또는 이미지별 수정 프롬프트를 입력해 주세요.";
    return false;
  }
  if (!apiResults || targets.some((target) => !target.image_id)) {
    status.textContent = previewFirstResult ? "미리보기에서는 수정 요청을 보내지 않습니다." : "이미지를 다시 생성한 뒤 수정해 주세요.";
    return false;
  }

  failedEdits = [];
  status.textContent = `이미지 ${targets.length}장을 수정하고 있습니다…`;
  showEditProgress(true);
  try {
    const images = targets.map(({ image_id, image_prompt }) => ({ image_id, image_prompt }));
    const response = await editAdImages(apiResults.session_id, common, images);
    const byId = new Map(targets.map((target) => [target.image_id, target]));
    for (const item of response.results) {
      const target = byId.get(item.image_id);
      if (item.image) apiResults.results[target.index] = item.image;
      else failedEdits.push({ target, message: item.error.message });
    }
    const delay = Math.max(0, ...response.results.map((item) => retryDelay(item.error?.retry_after)));
    if (delay) {
      editCooldownUntil = Date.now() + delay;
      setTimeout(() => showEditProgress(false), delay);
    }
    const completed = targets.length - failedEdits.length;
    const failure = failedEdits.length ? ` 실패 ${failedEdits.length}장. ${failedEdits[0].message}` : "";
    batchStatus.textContent = `수정 완료 ${completed}장.${failure}`;
    showResults();
    storeDraft();
    return true;
  } catch (error) {
    status.textContent = error.message;
    if (status === batchStatus && (!error.status || [429, 502, 504].includes(error.status))) {
      failedEdits = targets.map((target) => ({ target }));
    }
    if (error.retryDelay) {
      editCooldownUntil = Date.now() + error.retryDelay;
      setTimeout(() => showEditProgress(false), error.retryDelay);
    }
    return false;
  } finally {
    if (Date.now() >= editCooldownUntil) showEditProgress(false);
  }
}

applyEdits.addEventListener("click", () => {
  if (!commonPrompt.reportValidity()) return;
  const targets = failedEdits.length ? failedEdits.map(({ target }) => target) : editTargets();
  void submitImageEdits(targets, commonPrompt.value.trim(), batchStatus);
});

editForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (saveImagePrompt()) editDialog.close();
});

editDialog.addEventListener("cancel", (event) => {
  if (editing) event.preventDefault();
});
editDialog.querySelector(".auth-close").addEventListener("click", (event) => {
  if (editing) event.preventDefault();
});

const generationNotice = document.querySelector("#generation-notice");
const generationAction = generationNotice.querySelector("button");
const storageNotice = document.querySelector("#storage-notice");
const fieldNames = ["shop", "instagramId", "businessHours", "booking", "prompt"];
let retryTimer;
let generationRetryUntil = 0;
let replacePhotoAfterError = false;
let saveTimer;
let pendingSave = Promise.resolve();
let chatId = new URLSearchParams(location.search).get("chat");
const sidebarChatList = document.querySelector("#sidebar-chat-list");
const sidebarChatStatus = document.querySelector("#sidebar-chat-status");
const sidebarShop = document.querySelector("#sidebar-shop");
const sidebarShopForm = document.querySelector("#sidebar-shop-form");
const sidebarShopEdit = document.querySelector("#edit-sidebar-shop");
const sidebarShopStatus = document.querySelector("#sidebar-shop-status");
const submittedPrompt = document.querySelector("#submitted-prompt");
const SIDEBAR_CHAT_LIMIT = 5;
let sidebarChats = [];
let submittedFields;

const sidebarToggle = document.querySelector(".workspace-sidebar-toggle");
const sidebarMenu = sidebarToggle.parentElement;
const compactWorkspace = matchMedia("(max-width: 650px)");
let sidebarPinned = false;

// 좁은 화면에서 마우스로 미리 열거나 버튼으로 열린 상태를 유지합니다.
function setSidebarExpanded(expanded) {
  sidebarToggle.setAttribute("aria-expanded", String(expanded));
}

function closeSidebar() {
  sidebarPinned = false;
  setSidebarExpanded(false);
}

sidebarMenu.addEventListener("pointerenter", (event) => {
  if (compactWorkspace.matches && event.pointerType === "mouse") setSidebarExpanded(true);
});
sidebarMenu.addEventListener("pointerleave", (event) => {
  if (event.pointerType === "mouse" && !sidebarPinned && !sidebarMenu.contains(document.activeElement)) {
    setSidebarExpanded(false);
  }
});
sidebarToggle.addEventListener("click", () => {
  sidebarPinned = !sidebarPinned;
  setSidebarExpanded(sidebarPinned);
});
document.addEventListener("click", (event) => {
  if (!sidebarMenu.contains(event.target)) closeSidebar();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || sidebarToggle.getAttribute("aria-expanded") !== "true") return;
  if (sidebarMenu.contains(document.activeElement)) sidebarToggle.focus();
  closeSidebar();
});
compactWorkspace.addEventListener("change", closeSidebar);

// 전송한 매장 정보와 최초 생성 프롬프트를 표시합니다.
function showSubmittedFields(fields) {
  submittedFields = fields;
  for (const item of sidebarShop.querySelectorAll("[data-shop-field]")) {
    item.textContent = fields[item.dataset.shopField]?.trim() || "—";
  }
  submittedPrompt.textContent = fields.prompt ?? "";
  submittedPrompt.hidden = !submittedPrompt.textContent.trim();
  sidebarShop.hidden = false;
}

// 매장 정보 요약 자리에서 저장된 값을 직접 편집합니다.
function setShopEditing(editing) {
  sidebarShopForm.hidden = !editing;
  sidebarShop.querySelector("dl").hidden = editing;
  sidebarShopEdit.setAttribute("aria-expanded", String(editing));
  if (!editing) sidebarShopEdit.focus();
}

sidebarShopEdit.addEventListener("click", () => {
  if (!sidebarShopForm.hidden) return setShopEditing(false);
  for (const field of sidebarShopForm.querySelectorAll("input")) {
    field.value = submittedFields[field.name] ?? "";
  }
  sidebarShopStatus.textContent = "";
  setShopEditing(true);
  sidebarShopForm.elements.shop.focus();
});
document.querySelector("#cancel-sidebar-shop").addEventListener("click", () => setShopEditing(false));
sidebarShopForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = { ...submittedFields };
  for (const input of sidebarShopForm.querySelectorAll("input")) {
    input.value = input.value.trim();
    fields[input.name] = input.value;
  }
  if (!sidebarShopForm.reportValidity()) return;
  if (fields.instagramId.startsWith("@") || !/^https?:\/\/[^\s/?#]+/i.test(fields.booking)) {
    sidebarShopStatus.textContent = "계정명은 @ 없이, 예약 링크는 http:// 또는 https://로 입력해 주세요.";
    return;
  }
  const controls = Array.from(sidebarShopForm.elements);
  controls.forEach((control) => { control.disabled = true; });
  sidebarShopEdit.disabled = true;
  const saved = previewFirstResult || await storeDraft(fields);
  controls.forEach((control) => { control.disabled = false; });
  sidebarShopEdit.disabled = false;
  if (!saved) {
    sidebarShopStatus.textContent = "매장 정보를 저장하지 못했습니다. 다시 시도해 주세요.";
    return;
  }
  for (const input of sidebarShopForm.querySelectorAll("input")) form.elements[input.name].value = fields[input.name];
  showSubmittedFields(fields);
  setShopEditing(false);
});

// 채팅 목록에서 현재 작업을 표시하고 기존 복원 경로로 연결합니다.
function renderSidebarChats(chats) {
  sidebarChats = chats;
  const items = chats.slice(0, SIDEBAR_CHAT_LIMIT).map((chat) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = `./create.html?chat=${encodeURIComponent(chat.id)}`;
    link.textContent = chat.fields.shop?.trim() || "제목 없는 광고";
    if (chat.id === chatId) link.setAttribute("aria-current", "page");
    item.append(link);
    return item;
  });
  sidebarChatList.replaceChildren(...items);
  sidebarChatStatus.textContent = chats.length ? "" : "채팅 내역이 없습니다.";
}
let hasResults = false;

// 현재 입력으로 이미지 생성을 요청하고 결과나 재시도 안내를 표시합니다.
async function generateImages() {
  if (Date.now() < generationRetryUntil) return;
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  clearTimeout(retryTimer);
  generationNotice.hidden = true;
  const body = new FormData(form);
  showSubmittedFields(Object.fromEntries(fieldNames.map((name) => [name, body.get(name)])));
  scheduleSave();
  chatId ??= crypto.randomUUID();
  body.append("session_id", chatId);
  const photo = selectedPhoto;
  body.append("images", photo);
  button.disabled = true;
  button.textContent = "생성 중…";
  try {
    const generation = pendingSave.then(async () => {
      const result = await generateAd(body);
      savedPhoto = photo;
      return result;
    });
    pendingSave = generation.catch(() => {});
    apiResults = await generation;
    imagePrompts = apiResults.results.map((_, index) => imagePrompts[index] ?? "");
    failedEdits = [];
    batchStatus.textContent = "";
    hasResults = true;
    showResults();
    storeDraft();
  } catch (error) {
    generationNotice.querySelector("p").textContent = error.message;
    replacePhotoAfterError = error.status === 413;
    generationAction.textContent = replacePhotoAfterError ? "사진 교체" : "다시 시도";
    generationAction.hidden = false;
    generationAction.disabled = Boolean(error.retryDelay);
    generationNotice.hidden = false;
    if (error.retryDelay) {
      generationRetryUntil = Date.now() + error.retryDelay;
      generationAction.textContent = `다시 시도 (${Math.ceil(error.retryDelay / 1000)}초 후)`;
      retryTimer = setTimeout(() => {
        generationAction.disabled = false;
        generationAction.textContent = "다시 시도";
        button.disabled = false;
      }, error.retryDelay);
    }
  } finally {
    button.disabled = Date.now() < generationRetryUntil;
    button.textContent = "생성하기";
  }
}

generationAction.addEventListener("click", () => {
  if (replacePhotoAfterError) {
    photoInput.click();
    return;
  }
  form.requestSubmit();
});

// 연속 입력이 멈춘 뒤 한 번만 초안을 저장하도록 예약합니다.
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(storeDraft, 300);
}

// 현재 초안의 스냅샷을 만들고 앞선 저장이 끝난 뒤 서버에 보냅니다.
function storeDraft(shopUpdate) {
  clearTimeout(saveTimer);
  saveTimer = undefined;
  if (previewFirstResult) return pendingSave;
  const fields = Object.fromEntries(fieldNames.map((name) => [name, form.elements[name].value]));
  if (shopUpdate) for (const input of sidebarShopForm.querySelectorAll("input")) fields[input.name] = shopUpdate[input.name];
  if (!chatId && !selectedPhoto && !Object.values(fields).some((value) => value.trim())) return;

  chatId ??= crypto.randomUUID();
  const chat = {
    id: chatId, fields, photo: selectedPhoto, submittedFields: shopUpdate ?? submittedFields,
    commonPrompt: commonPrompt.value, imagePrompts: (apiResults?.results ?? []).flatMap((image, index) =>
      image.type === "nail_tip" ? [imagePrompts[index] ?? ""] : []),
    priceTablePrompt: imagePrompts[(apiResults?.results ?? []).findIndex((image) => image.type === "price_table")] ?? "",
    publishDraft: getPublishDraft(),
  };
  pendingSave = pendingSave.then(async () => {
    try {
      await saveChat(chat, savedPhoto);
      savedPhoto = chat.photo;
      renderSidebarChats([{ id: chat.id, fields }, ...sidebarChats.filter((entry) => entry.id !== chat.id)]);
      const page = new URL(location.href);
      if (!page.searchParams.has("chat")) {
        page.searchParams.set("chat", chat.id);
        history.replaceState(null, "", page);
      }
      storageNotice.textContent = "";
      return true;
    } catch {
      storageNotice.textContent = "채팅을 저장하지 못했습니다.";
      return false;
    }
  });
  return pendingSave;
}

// 저장된 초안과 사진, 생성 결과를 읽어 작업 화면에 복원합니다.
async function restoreChat() {
  try {
    const { chats, chatLimit } = await listChats();
    renderSidebarChats(chats);
    if (!chatId && chats.length >= chatLimit) {
      location.replace("./mypage.html?new");
      return;
    }
    if (!chatId) return;
    if (!chats.some((entry) => entry.id === chatId)) {
      storageNotice.textContent = "이 계정에서 해당 채팅을 찾을 수 없습니다.";
      return;
    }
  } catch {
    sidebarChatStatus.textContent = "채팅 내역을 불러오지 못했습니다.";
    return;
  }

  try {
    const chat = await loadChat(chatId);
    for (const name of fieldNames) form.elements[name].value = chat.fields[name] ?? "";
    if (chat.photo) selectPhoto([chat.photo], { restored: true });
    validatePrompt(prompt, promptCount);
    hasResults = chat.hasResults;
    apiResults = chat.apiResults;
    commonPrompt.value = chat.commonPrompt ?? "";
    validatePrompt(commonPrompt, commonPromptCount);
    let promptIndex = 0;
    imagePrompts = (apiResults?.results ?? []).map((image) =>
      image.type === "price_table" ? (chat.priceTablePrompt ?? "") : (chat.imagePrompts?.[promptIndex++] ?? ""));
    restorePublishDraft(chat.publishDraft ?? { caption: chat.caption });
    if (chat.submittedFields || hasResults) showSubmittedFields(chat.submittedFields ?? chat.fields);
    if (hasResults) showResults();
    storageNotice.textContent = "";
  } catch {
    storageNotice.textContent = "저장한 채팅을 불러오지 못했습니다.";
  }
}

document.addEventListener("chat-draft-change", scheduleSave);
document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[href]");
  if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  if (saveTimer) storeDraft();
  await pendingSave;
  location.assign(link.href);
});
document.addEventListener("onecut-authenticated", restoreChat);
if (previewFirstResult) {
  const fields = {
    shop: "원컷 네일샵", instagramId: "onecut_nail", businessHours: "화~일 11:00–20:00",
    booking: "https://example.com/booking", prompt: "3세트(팁 15개), 왼쪽부터 39,000원 / 49,000원 / 59,000원",
  };
  showSubmittedFields(fields);
  sidebarChatStatus.textContent = "채팅 내역이 없습니다.";
  apiResults = {
    results: [{ type: "price_table" }, ...Array.from({ length: 3 }, () => ({ type: "nail_tip" }))],
    previewCopy: {
      caption: "원컷 네일샵의 이달의 아트를 소개합니다 💅\n왼쪽부터 39,000원, 49,000원, 59,000원이에요.\n화~일 11:00–20:00 · 예약 문의 @onecut_nail",
      hashtags: "#원컷네일샵 #이달의아트 #네일아트 #네일디자인",
    },
  };
  hasResults = true;
  showResults();
} else {
  void restoreChat();
}
