/** 선택 입력, 첨부 사진 및 생성 유형별 실험 결과를 관리합니다. */
const form = document.querySelector('#experiment');
const tabs = [...document.querySelectorAll('[role="tab"]')];
const fileInput = document.querySelector('#images');
const dropZone = document.querySelector('#drop-zone');
const attachmentList = document.querySelector('#attachments');
const attachmentStatus = document.querySelector('#attachment-status');
const panel = document.querySelector('#results');
const generate = document.querySelector('#generate');
const runs = {image: [], text: [], both: []};
const pending = {image: false, text: false, both: false};
const runCounts = {image: 0, text: 0, both: 0};
const clearResults = document.querySelector('#clear-results');
const files = [];
const imageTypes = new Set(['image/jpeg', 'image/png']);
const fieldLabels = {
  shop: '매장명', instagramId: '인스타그램 아이디', businessHours: '영업시간',
  booking: '예약 링크', prompt: '프롬프트', images: '사진',
};
let mode = 'image';

function showMode() {
  for (const tab of tabs) {
    const selected = tab.dataset.mode === mode;
    tab.setAttribute('aria-selected', selected);
    tab.tabIndex = selected ? 0 : -1;
  }
  const resultKinds = {image: '네일 팁 가격표와 착용컷', text: '게시글 본문과 해시태그', both: '이미지와 게시글 문구'};
  const buttonLabels = {image: '이미지 생성하기', text: '글 생성하기', both: '이미지와 글 생성하기'};
  const modes = mode === 'both' ? ['image', 'text'] : [mode];
  const connected = modes.filter((kind) => modelEndpoints[kind]).length;
  panel.setAttribute('aria-labelledby', 'tab-' + mode);
  document.querySelector('#result-kind').textContent = resultKinds[mode];
  document.querySelector('#connection').textContent = connected === modes.length ? 'API 연결 모드' :
    connected ? 'API · 목업 혼합' : '목업 모드';
  generate.textContent = pending[mode] ? '생성 중…' : buttonLabels[mode];
  generate.disabled = pending[mode];
  clearResults.disabled = runs[mode].length === 0;
  document.querySelector('#empty').hidden = runs[mode].length > 0;
  document.querySelector('#runs').replaceChildren(...runs[mode]);
}

function showAttachments() {
  globalThis.modelLabFiles.renderAttachments(attachmentList, files, (entry, index) => {
    URL.revokeObjectURL(entry.url);
    files.splice(index, 1);
    showAttachments();
  });
}

function addFiles(items) {
  let skipped = false;
  for (const file of items) {
    if (!imageTypes.has(file.type)) {
      skipped = true;
      continue;
    }
    files.push({file, url: URL.createObjectURL(file)});
  }
  attachmentStatus.textContent = skipped ? 'JPG와 PNG 파일만 첨부할 수 있습니다.' : '';
  showAttachments();
}

function showResult(output, result, targetMode) {
  if (targetMode === 'image') {
    const gallery = document.createElement('div');
    gallery.className = 'gallery';
    for (const [index, item] of result.images.entries()) {
      const figure = document.createElement('figure');
      const image = new Image();
      image.alt = item.label || '결과 이미지 ' + (index + 1);
      image.src = item.url;
      const caption = document.createElement('figcaption');
      caption.textContent = image.alt;
      image.addEventListener('error', () => {caption.textContent = image.alt + ' · 이미지를 불러오지 못했습니다.';});
      figure.append(image, caption);
      gallery.append(figure);
    }
    output.append(gallery);
    return;
  }
  for (const value of [result.caption, result.hashtags]) {
    if (!value) continue;
    const paragraph = document.createElement('p');
    paragraph.textContent = value;
    output.append(paragraph);
  }
}

async function exportResult(entry) {
  if (!entry.result?.images) return entry;
  const images = await Promise.all(entry.result.images.map(async (item) => {
    if (item.url.startsWith('data:')) return item;
    try {
      const response = await fetch(item.url);
      if (!response.ok) throw new Error('이미지 다운로드 실패');
      return {...item, dataUrl: await globalThis.modelLabFiles.readDataUrl(await response.blob())};
    } catch {
      return {...item, download: 'url-only'};
    }
  }));
  return {...entry, result: {...entry.result, images}};
}

async function downloadRun(record, photos, button, status) {
  button.disabled = true;
  status.textContent = '실험 기록을 준비 중입니다.';
  try {
    const images = await Promise.all(photos.map(async (photo) => ({
      name: photo.name, type: photo.type, lastModified: photo.lastModified,
      dataUrl: await globalThis.modelLabFiles.readDataUrl(photo),
    })));
    const results = await Promise.all(record.results.map(exportResult));
    const json = JSON.stringify({...record, images, results}, null, 2);
    const url = URL.createObjectURL(new Blob([json], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'nail-experiment-' + record.mode + '-' + record.startedAt.replaceAll(':', '-') + '.json';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const urlOnly = results.some((entry) => entry.result?.images?.some((image) => image.download === 'url-only'));
    status.textContent = urlOnly ? '기록을 다운로드했습니다. 일부 결과 이미지는 URL로 저장했습니다.' : '실험 기록을 다운로드했습니다.';
  } catch {
    status.textContent = '실험 기록을 저장하지 못했습니다. 다시 시도해 주세요.';
  } finally {
    button.disabled = false;
  }
}

for (const tab of tabs) {
  tab.addEventListener('click', () => {
    mode = tab.dataset.mode;
    showMode();
  });
  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    const index = (tabs.indexOf(tab) + direction + tabs.length) % tabs.length;
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[index];
    next.click();
    next.focus();
  });
}

document.querySelector('#attach').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  addFiles(fileInput.files);
  fileInput.value = '';
});
dropZone.addEventListener('dragover', (event) => {
  event.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove('drag-over');
});
dropZone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropZone.classList.remove('drag-over');
  addFiles(event.dataTransfer.files);
});
const clearButtons = [...form.querySelectorAll('[data-clear]')];

function syncClearButtons() {
  for (const button of clearButtons) {
    button.hidden = !form.elements[button.dataset.clear].value;
  }
}

for (const button of clearButtons) {
  button.addEventListener('click', () => {
    const field = form.elements[button.dataset.clear];
    field.value = '';
    field.focus();
    syncClearButtons();
  });
}
form.addEventListener('input', syncClearButtons);
form.addEventListener('reset', (event) => {
  event.preventDefault();
  for (const button of clearButtons) form.elements[button.dataset.clear].value = '';
  for (const entry of files) URL.revokeObjectURL(entry.url);
  files.length = 0;
  fileInput.value = '';
  attachmentStatus.textContent = '';
  document.querySelector('#import-status').hidden = true;
  showAttachments();
  syncClearButtons();
});

function readExperiment(record) {
  const invalid = () => new Error('지원하는 실험 기록 JSON 형식이 아닙니다.');
  if (record?.schemaVersion !== 1 || !Object.hasOwn(runs, record.mode) ||
      !record.input || !Array.isArray(record.images)) throw invalid();
  const input = {};
  for (const button of clearButtons) {
    const name = button.dataset.clear;
    const value = record.input[name];
    if (value !== undefined && typeof value !== 'string') throw invalid();
    input[name] = value ?? '';
  }
  const images = record.images.map((image) => {
    const match = typeof image?.dataUrl === 'string' &&
      /^data:(image\/[a-z0-9.+-]+);base64,([a-z0-9+/]*={0,2})$/i.exec(image.dataUrl);
    if (!match || typeof image.name !== 'string' || !image.name) throw invalid();
    const bytes = Uint8Array.from(atob(match[2]), (character) => character.charCodeAt(0));
    const options = {type: match[1]};
    if (Number.isFinite(image.lastModified)) options.lastModified = image.lastModified;
    return new File([bytes], image.name, options);
  });
  return {mode: record.mode, input, images};
}

const recordFile = document.querySelector('#record-file');
document.querySelector('#import-record').addEventListener('click', () => recordFile.click());
recordFile.addEventListener('change', async () => {
  const file = recordFile.files[0];
  if (!file) return;
  recordFile.value = '';
  const status = document.querySelector('#import-status');
  status.hidden = false;
  status.classList.remove('error');
  try {
    const restored = readExperiment(JSON.parse(await file.text()));
    for (const entry of files) URL.revokeObjectURL(entry.url);
    files.length = 0;
    for (const [name, value] of Object.entries(restored.input)) form.elements[name].value = value;
    for (const file of restored.images) files.push({file, url: URL.createObjectURL(file)});
    attachmentStatus.textContent = '';
    showAttachments();
    mode = restored.mode;
    syncClearButtons();
    showMode();
    status.textContent = '실험 입력을 복원했습니다. 생성 버튼을 누르면 다시 실행됩니다.';
  } catch {
    status.classList.add('error');
    status.textContent = '실험 기록을 불러오지 못했습니다. 이 페이지에서 다운로드한 JSON 파일을 선택해 주세요.';
  }
});

document.querySelector('#sample').addEventListener('click', () => {
  form.elements.shop.value = '오라네일';
  form.elements.instagramId.value = 'ora_nail';
  form.elements.businessHours.value = '11:00–20:00';
  form.elements.booking.value = 'https://example.com/reserve';
  form.elements.prompt.value = '겨울 느낌의 이달의 아트를 만들어 주세요. 네 디자인 가격은 각각 39,000원입니다.';
  syncClearButtons();
});

clearResults.addEventListener('click', () => {
  runs[mode].length = 0;
  runCounts[mode] = 0;
  showMode();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const targetMode = mode;
  const startedAt = new Date().toISOString();
  if (pending[targetMode]) return;
  const input = Object.fromEntries(new FormData(form));
  input.instagramId = input.instagramId.trim().replace(/^@+/, '');
  input.booking = input.booking.trim();
  const bookingUrl = URL.parse(input.booking);
  if (input.booking && (!/^https?:\/\//i.test(input.booking) ||
      !bookingUrl || !['http:', 'https:'].includes(bookingUrl.protocol))) {
    const booking = form.elements.booking;
    booking.setCustomValidity('http:// 또는 https://로 시작하는 예약 링크를 입력해 주세요.');
    booking.reportValidity();
    booking.setCustomValidity('');
    return;
  }
  const images = files.map(({file}) => file);
  if (images.some((file) => !imageTypes.has(file.type))) {
    attachmentStatus.textContent = '다시 실행하려면 JPG·PNG 이외의 첨부 파일을 제거해 주세요.';
    return;
  }
  const run = document.querySelector('#run-template').content.firstElementChild.cloneNode(true);
  const title = '실험 ' + (++runCounts[targetMode]);
  run.querySelector('h3').textContent = title;
  const deleteButton = run.querySelector('.delete-run');
  deleteButton.setAttribute('aria-label', title + ' 삭제');
  deleteButton.addEventListener('click', () => {
    runs[targetMode] = runs[targetMode].filter((entry) => entry !== run);
    showMode();
  });
  const status = run.querySelector('.run-status');
  const details = run.querySelector('dl');
  for (const [key, label] of Object.entries(fieldLabels)) {
    const term = document.createElement('dt');
    term.textContent = label;
    const value = document.createElement('dd');
    value.textContent = (key === 'images' ? images.map((image) => image.name).join(', ') : input[key]) || '미입력';
    details.append(term, value);
  }
  status.textContent = '생성 중';
  runs[targetMode].unshift(run);
  pending[targetMode] = true;
  showMode();

  const output = run.querySelector('.output');
  const combined = targetMode === 'both';
  const kinds = combined ? ['image', 'text'] : [targetMode];
  const endpoints = Object.fromEntries(kinds.map((kind) => [kind, modelEndpoints[kind] || null]));
  try {
    const results = await Promise.all(kinds.map(async (kind) => {
      const section = document.createElement('section');
      if (combined) {
        const heading = document.createElement('h4');
        heading.textContent = kind === 'image' ? '이미지 결과' : '글 결과';
        section.append(heading);
        section.className = 'combined-output';
      }
      output.append(section);
      try {
        const result = await requestGeneration(kind, input, images);
        showResult(section, result, kind);
        if (combined && !modelEndpoints[kind]) {
          const note = document.createElement('p');
          note.className = 'output-note';
          note.textContent = '목업 결과';
          section.append(note);
        }
        return {kind, source: endpoints[kind] ? 'api' : 'mock', result};
      } catch (error) {
        const message = document.createElement('p');
        message.className = 'run-status error';
        message.textContent = error.message || '생성 요청에 실패했습니다.';
        section.append(message);
        return {kind, source: endpoints[kind] ? 'api' : 'mock', error: message.textContent};
      }
    }));
    const download = run.querySelector('.download-run');
    download.hidden = false;
    download.addEventListener('click', () => downloadRun(
      {schemaVersion: 1, startedAt, mode: targetMode, input, endpoints, results},
      images, download, run.querySelector('.download-status'),
    ));
    const count = results.filter((entry) => entry.result).length;
    status.textContent = count === kinds.length
      ? (kinds.some((kind) => modelEndpoints[kind]) ? '완료' : '목업 결과')
      : count ? '일부 완료' : '생성 실패';
    status.classList.toggle('error', !count);
  } finally {
    pending[targetMode] = false;
    showMode();
  }
});

showMode();
syncClearButtons();
