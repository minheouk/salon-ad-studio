/** 광고 모델 실험 화면에서 입력, 수정 기록, 결과 다운로드를 관리합니다. */

import { generateAd } from "./model-client.js";

const lab = document.querySelector(".lab");
const session = document.querySelector("#session");
const history = document.querySelector("#history");
const welcome = document.querySelector("#welcome");
const composer = document.querySelector("#composer");
const prompt = document.querySelector("#prompt");
const imageModel = document.querySelector("#image-model");
const images = document.querySelector("#images");
const attach = document.querySelector("#attach");
const attachments = document.querySelector("#attachments");
const submit = document.querySelector("#submit");
const status = document.querySelector("#status");
const error = document.querySelector("#error");
const turn = document.querySelector("#turn-template");
const imageTypes = new Set(["image/png", "image/jpeg"]);

let draftImages = [];
let resultImage = null;
let resultCount = 0;
let pending = null;
const imageUrls = new Set();

// 화면에서 사용하는 URL을 기록해 실험 종료 시 해제합니다.
function previewUrl(blob) {
  const url = URL.createObjectURL(blob);
  imageUrls.add(url);
  return url;
}

// 입력 상태에 맞게 생성 또는 수정 요청 버튼을 갱신합니다.
function updateComposer() {
  prompt.placeholder = resultImage ? "수정할 내용을 설명해 주세요" : "만들고 싶은 광고를 설명해 주세요";
  submit.textContent = resultImage ? "수정 요청" : "보내기";
  submit.disabled = Boolean(pending) || !prompt.value.trim() || !imageModel.value;
  imageModel.disabled = Boolean(pending);
  attach.disabled = Boolean(pending);
  prompt.readOnly = Boolean(pending);
  status.hidden = !pending;
}

// 첨부 이미지를 미리 보고 제출 전에는 제거할 수 있게 합니다.
function renderAttachments(container, entries, removable = false) {
  container.hidden = entries.length === 0;
  globalThis.modelLabFiles.renderAttachments(container, entries, removable ? (entry) => {
    draftImages = draftImages.filter((candidate) => candidate !== entry);
    URL.revokeObjectURL(entry.url);
    imageUrls.delete(entry.url);
    renderAttachments(attachments, draftImages, true);
    attach.focus();
  } : undefined);
}

// 오류가 발생하면 입력을 유지한 상태에서 메시지를 보여줍니다.
function showError(message = "") {
  error.textContent = message;
  error.hidden = !message;
}

// 생성된 이미지와 사용자가 입력한 내용을 기록으로 표시합니다.
function addTurn(text, entries, blob) {
  const item = turn.content.cloneNode(true);
  item.querySelector(".request p").textContent = text;
  renderAttachments(item.querySelector(".attachments"), entries);
  const image = item.querySelector(".result");
  image.src = previewUrl(blob);
  image.alt = `광고 실험 결과 ${++resultCount}`;
  const download = item.querySelector("a");
  download.href = image.src;
  download.download = `ad-result-${resultCount}.png`;
  history.append(item);
}

// 결과 화면을 열거나 초기 입력 화면으로 돌아갑니다.
function showSession(active) {
  lab.classList.toggle("active", active);
  session.hidden = !active;
  history.hidden = !active;
  welcome.hidden = active;
  prompt.focus({ preventScroll: true });
}

// 파일 선택과 드롭에 같은 첨부 검증을 적용합니다.
function addImages(files) {
  if (pending || !files.length) return;
  const supported = files.filter((file) => imageTypes.has(file.type));
  showError(supported.length < files.length ? "JPG와 PNG 이미지만 첨부할 수 있습니다." : "");
  draftImages.push(...supported.map((file) => ({ file, url: previewUrl(file) })));
  renderAttachments(attachments, draftImages, true);
}

attach.addEventListener("click", () => images.click());
images.addEventListener("change", () => {
  addImages(Array.from(images.files));
  images.value = "";
});

composer.addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = pending ? "none" : "copy";
  composer.classList.toggle("drag-over", !pending);
});
composer.addEventListener("dragleave", (event) => {
  if (!composer.contains(event.relatedTarget)) composer.classList.remove("drag-over");
});
composer.addEventListener("drop", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  composer.classList.remove("drag-over");
  addImages(Array.from(event.dataTransfer.files));
});
imageModel.addEventListener("change", updateComposer);
prompt.addEventListener("input", updateComposer);
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = prompt.value.trim();
  if (!text || pending || !imageModel.value) return;

  const entries = draftImages.slice();
  const request = {
    prompt: text,
    additionalImages: entries.map(({ file }) => file),
    resultImage,
    imageModel: imageModel.value,
  };
  pending = request;
  composer.classList.remove("drag-over");
  showError();
  status.textContent = resultImage ? "광고를 수정하고 있습니다…" : "광고를 생성하고 있습니다…";
  attachments.inert = true;
  showSession(true);
  updateComposer();

  try {
    const blob = await generateAd(request);
    if (pending !== request) return;
    addTurn(text, entries, blob);
    resultImage = blob;
    prompt.value = "";
    draftImages = [];
    renderAttachments(attachments, draftImages);
    window.scrollTo(0, document.documentElement.scrollHeight);
  } catch (cause) {
    if (pending !== request) return;
    showError(cause.message || "광고를 만들지 못했습니다. 다시 시도해 주세요.");
  } finally {
    if (pending === request) {
      pending = null;
      attachments.inert = false;
      updateComposer();
    }
  }
});

document.querySelector("#reset").addEventListener("click", () => {
  pending = null;
  resultImage = null;
  resultCount = 0;
  draftImages = [];
  prompt.value = "";
  images.value = "";
  history.replaceChildren();
  renderAttachments(attachments, draftImages);
  attachments.inert = false;
  imageUrls.forEach((url) => URL.revokeObjectURL(url));
  imageUrls.clear();
  showError();
  updateComposer();
  showSession(false);
});
