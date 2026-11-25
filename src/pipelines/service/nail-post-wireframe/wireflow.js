/** 네일 게시물 와이어플로우와 화면별 클릭 체험을 구성합니다. */
const screens = [
  {id: 'input', code: 'N01', title: '게시물 생성', x: 30, y: 70},
  {id: 'draft', code: 'N02', title: '첫 장 · 네일 팁과 가격', x: 355, y: 70},
  {id: 'collage', code: 'N03', title: '두 번째 장 · 착용컷 콜라주', x: 680, y: 70},
  {id: 'review', code: 'N04', title: '두 장 최종 검토 · 게시하기', x: 1005, y: 70},
  {id: 'done', code: 'N05', title: '게시 완료', x: 1330, y: 70},
  {id: 'image', code: 'N06', title: '이미지 수정 → 초안 복귀', x: 192, y: 727},
  {id: 'copy', code: 'N07', title: '문구 수정 → 초안 복귀', x: 518, y: 727},
];
const canvasWidth = 1604;
const canvasHeight = 1170;
const sample = {
  photo: 'winter-nail-tips.png', prices: [39000, 39000, 39000, 39000],
  shop: '오라네일', about: '', booking: 'https://example.com/reserve',
  caption: '깊은 버건디부터 은은한 그린·라벤더까지 ❄️\n반짝이는 실버와 리본, 별 장식을 더한 겨울 아트를 만나보세요.',
  tags: '#이달의아트 #이달의네일 #네일아트', side: 'right', history: [],
};
let draft = {...sample};
let zoom = 1;
const attachments = [];
const shopFields = {
  shop: '매장명', instagramId: '인스타그램 아이디', businessHours: '영업시간', booking: '예약 링크',
};
const exampleRequest = '겨울 무드로 이달의 아트를 만들어 주세요. 네 디자인 모두 39,000원입니다.';
const byId = (id) => document.getElementById(id);
const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);

function link(id, label, primary = false) {
  return `<a class="button${primary ? ' primary' : ''}" href="#${id}">${label}</a>`;
}

function field(name, label, value, type = 'text', required = true) {
  return `<label class="field">${label}<input name="${name}" type="${type}" value="${escape(value)}"${type === 'number' ? ' min="0" step="1"' : ''}${required ? ' required' : ''}></label>`;
}

function collage(data) {
  if (data.sampleArtwork !== false && data.prices.length === 4 && data.prices.every((price) => price === 39000)) {
    return '<img class="collage-artwork" src="collage-example.png" alt="겨울 네일 착용컷 네 장과 공통가 39,000원을 표시한 콜라주">';
  }
  const count = Math.max(data.prices.length, data.photos?.length || 0, 1);
  const shared = count > 1 && data.prices.length === count && data.prices.every((price) => price === data.prices[0]);
  const tiles = Array.from({length: count}, (_, index) => {
    const price = data.prices[index];
    const label = shared || price === undefined ? '' : '<small>' + price.toLocaleString('ko-KR') + '원</small>';
    return '<span>디자인 ' + (index + 1) + ' 착용컷' + label + '</span>';
  }).join('');
  const badge = shared ? '<b>' + data.prices[0].toLocaleString('ko-KR') + '원</b>' : '';
  return '<div class="collage"><h3>이달의 아트</h3><div class="collage-grid">' + tiles + badge + '</div></div>';
}

function post(data, slide = 0) {
  const prices = data.prices.map((price) => `<li>${price.toLocaleString('ko-KR')}원</li>`).join('');
  const photos = (data.photos || [data.photo]).map((source) => `<img src="${escape(source)}" alt="첨부 네일 디자인">`).join('');
  return `<article class="post"><header><b>${escape(data.shop || '매장명 미입력')}</b> · 이달의 아트</header>
    <section class="carousel" aria-label="게시물 이미지 두 장" data-slide="${slide}" tabindex="0">
      <div data-page="0" ${slide === 0 ? '' : 'hidden'}>
        <div class="photo ${data.side === 'left' ? 'reverse' : ''}"><div class="post-images">${photos || '<p class="hint">첨부 사진 없음</p>'}</div><ol aria-label="입력 순서대로 가격">${prices}</ol></div>
      </div>
      <div data-page="1" ${slide === 1 ? '' : 'hidden'}>${collage(data)}</div>
      <div class="carousel-controls">
        <button type="button" data-slide-to="0" aria-label="이전 이미지" ${slide === 0 ? 'disabled' : ''}>‹</button>
        <span data-slide-label aria-live="polite">${slide === 0 ? '1 / 2 · 네일 팁과 가격' : '2 / 2 · 착용컷 콜라주'}</span>
        <button type="button" data-slide-to="1" aria-label="다음 이미지" ${slide === 1 ? 'disabled' : ''}>›</button>
      </div>
    </section>
    <div class="caption">${escape(data.caption)}<small>${escape(data.tags)}</small>${data.booking ? `<small>예약  ${escape(data.booking)}</small>` : ''}</div></article>`;
}

function history(data) {
  return data.history.map((message) => `<p class="bubble mine">${escape(message)}</p>`).join('');
}

function content(id, data) {
  switch (id) {
    case 'input':
      return `<div class="chat-start">
        <h3>이달의 아트 만들기</h3>
        <form class="composer experiment-form" data-form="input">
          <p class="optional-note">모든 입력은 선택입니다.</p>
          <div class="fields">${Object.entries(shopFields).map(([name, label]) => field(name, label, '', 'text', false)).join('')}</div>
          <div class="attachments" aria-label="첨부 이미지"></div>
          <label class="field">프롬프트<textarea name="prompt" rows="3" placeholder="원하는 분위기, 가격, 결과 형식을 자유롭게 적어 주세요."></textarea></label>
          <input name="photo" type="file" accept="image/*" multiple hidden>
          <div class="actions"><button type="button" data-attach>＋ 사진 첨부</button><button class="primary">생성하기</button></div>
        </form>
        <button type="button" class="sample" data-sample>예시로 시작</button>
        <p class="composer-status" role="status"></p>
        </div>`;
    case 'draft':
      return `${data.request ?  `<p class="bubble mine request">${escape(data.request)}</p>` : ''}<p class="bubble">가격, 이미지, 문구를 확인해 주세요.</p>${history(data)}${post(data)}
        <div class="actions">${link('image','이미지 수정')}${link('copy','문구 수정')}${link('review','검토 완료',true)}</div><div>${link('collage','두 번째 이미지 보기')}</div>`;
    case 'collage':
      return '<p class="bubble">같은 게시물의 두 번째 이미지입니다.</p>' +
        post(data, 1) + '<div class="actions">' +
        link('draft', '첫 번째 이미지') + link('review', '두 장 검토', true) + '</div>';
    case 'image':
      return `<p class="bubble mine">이미지의 가격 표시를 수정할게요.</p>${history(data)}
        <form class="screen-body" data-form="image" style="padding:0">
          <label class="field">가격 (한 줄에 하나)<textarea name="prices" rows="4">${data.prices.join('\n')}</textarea></label>
          <label class="field">가격 표시 위치<select name="side"><option value="right">이미지 오른쪽</option><option value="left" ${data.side === 'left' ? 'selected' : ''}>이미지 왼쪽</option></select></label>
          <div class="actions">${link('draft','취소')}<button class="primary">이미지 수정 적용</button></div>
        </form>`;
    case 'copy':
      return `<p class="bubble mine">게시글의 문구를 수정할게요.</p>${history(data)}
        <form class="screen-body" data-form="copy" style="padding:0">
          <label class="field">본문<textarea name="caption" rows="5" required>${escape(data.caption)}</textarea></label>
          <label class="field">해시태그<textarea name="tags" rows="2">${escape(data.tags)}</textarea></label>
          ${field('booking','예약 링크',data.booking,'url')}
          <div class="actions">${link('draft','취소')}<button class="primary">문구 수정 적용</button></div>
        </form>`;
    case 'review':
      return `<p class="bubble">이미지 두 장을 한 게시물로 올릴까요?</p>${post(data)}
        <div class="actions">${link('draft','초안으로')}${link('done','게시하기',true)}</div>`;
    case 'done':
      return `<div class="done"><strong>✓</strong><h3>게시 완료</h3><p>${escape(data.shop)} · 이달의 아트</p><p>이미지 2장 · 인스타그램 게시 시뮬레이션</p></div>
        ${link('input','게시물 정보로 돌아가기')}`;
  }
}

function frame(screen, data, forBoard) {
  const style = forBoard ? ` style="left:${screen.x}px;top:${screen.y}px"` : '';
  const title = `<span>${screen.code}</span>${screen.title}`;
  return `<article class="${forBoard ? 'frame' : ''}"${style}>
    ${forBoard ? `<a class="frame-title" href="#${screen.id}">${title}</a>` : `<h2 class="frame-title">${title}</h2>`}
    <div class="screen"><div class="screen-head">이달의 아트 · ${screen.id === 'done' ? '게시 상태' : '게시물 제작 채팅'}</div>
      <div class="screen-body"${forBoard ? ' inert' : ''}>${content(screen.id, data)}</div>
    </div>
  </article>`;
}

function setZoom(value) {
  zoom = Math.min(1.8, Math.max(.25, value));
  byId('world').style.transform = `scale(${zoom})`;
  byId('bounds').style.width = `${canvasWidth * zoom}px`;
  byId('bounds').style.height = `${canvasHeight * zoom}px`;
  byId('zoom-level').value = `${Math.round(zoom * 100)}%`;
}

function fit() {
  const viewport = byId('viewport');
  setZoom(Math.min(1, viewport.clientWidth / canvasWidth, viewport.clientHeight / canvasHeight));
  viewport.scrollTo(0, 0);
}

function render() {
  const current = screens.find((screen) => screen.id === location.hash.slice(1));
  byId('board').hidden = Boolean(current);
  byId('player').hidden = !current;
  byId('board-link').toggleAttribute('aria-current', !current);
  byId('screen-link').toggleAttribute('aria-current', Boolean(current));
  if (!current) return;
  byId('index').innerHTML = [...screens].sort((a, b) => a.code.localeCompare(b.code)).map(
    ({id, code, title}) => `<a href="#${id}"${id === current.id ? ' aria-current="page"' : ''}>${code} · ${title}</a>`
  ).join('');
  byId('active-frame').innerHTML = frame(current, draft, false);
  if (current.id === 'input') showAttachments();
}

const activeFrame = byId('active-frame');

function showAttachments() {
  const preview = activeFrame.querySelector('.attachments');
  if (!preview) return;
  preview.innerHTML = attachments.map((entry, index) =>
    `<span><img src="${escape(entry.url)}" alt="${escape(entry.label)}"><button type="button" data-remove="${index}" aria-label="첨부 이미지 제거">×</button></span>`
  ).join('');
}

function attachFiles(files) {
  for (const file of files) {
    if (file.type.startsWith('image/')) attachments.push({url: URL.createObjectURL(file), label: file.name, file});
  }
  showAttachments();
}

function selectSlide(carousel, index) {
  carousel.dataset.slide = index;
  carousel.querySelectorAll('[data-page]').forEach((page) => {
    page.hidden = Number(page.dataset.page) !== index;
  });
  carousel.querySelectorAll('[data-slide-to]').forEach((button) => {
    button.disabled = Number(button.dataset.slideTo) === index;
  });
  carousel.querySelector('[data-slide-label]').textContent =
    index === 0 ? '1 / 2 · 네일 팁과 가격' : '2 / 2 · 착용컷 콜라주';
}

let swipeStart = null;
activeFrame.addEventListener('touchstart', (event) => {
  if (!event.target.closest('.carousel') || event.touches.length !== 1) return;
  const touch = event.touches[0];
  swipeStart = {x: touch.clientX, y: touch.clientY};
}, {passive: true});

activeFrame.addEventListener('touchend', (event) => {
  const carousel = event.target.closest('.carousel');
  const start = swipeStart;
  swipeStart = null;
  if (!carousel || !start) return;
  const touch = event.changedTouches[0];
  const dx = touch.clientX - start.x;
  const dy = touch.clientY - start.y;
  if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) selectSlide(carousel, dx < 0 ? 1 : 0);
}, {passive: true});

activeFrame.addEventListener('touchcancel', () => { swipeStart = null; });

activeFrame.addEventListener('keydown', (event) => {
  const carousel = event.target.closest('.carousel');
  if (!carousel || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  selectSlide(carousel, event.key === 'ArrowRight' ? 1 : 0);
});

activeFrame.addEventListener('click', (event) => {
  const slide = event.target.closest('[data-slide-to]');
  if (slide) selectSlide(slide.closest('.carousel'), Number(slide.dataset.slideTo));
  if (event.target.closest('[data-attach]')) activeFrame.querySelector('[name="photo"]').click();
  if (event.target.closest('[data-sample]')) {
    attachments.splice(0, attachments.length, {url: sample.photo, label: '예시 네일 사진'});
    const form = activeFrame.querySelector('.experiment-form');
    form.elements.prompt.value = exampleRequest;
    Object.assign(form.elements.shop, {value: sample.shop});
    form.elements.instagramId.value = 'ora_nail';
    form.elements.businessHours.value = '11:00–20:00';
    form.elements.booking.value = sample.booking;
    showAttachments();
  }
  if (event.target.matches('[data-remove]')) {
    attachments.splice(Number(event.target.dataset.remove), 1);
    showAttachments();
  }
});

activeFrame.addEventListener('change', (event) => {
  if (event.target.name !== 'photo') return;
  attachFiles(event.target.files);
  event.target.value = '';
});

for (const type of ['dragover', 'dragleave', 'drop']) {
  activeFrame.addEventListener(type, (event) => {
    const composer = activeFrame.querySelector('.composer');
    if (!composer) return;
    event.preventDefault();
    composer.classList.toggle('drag-over', type === 'dragover');
    if (type === 'drop') attachFiles(event.dataTransfer.files);
  });
}

activeFrame.addEventListener('submit', (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const type = event.target.dataset.form;
  if (type === 'input') {
    const fields = Object.fromEntries(Object.keys(shopFields).map((name) => [name, form.get(name).trim()]));
    const prompt = form.get('prompt').trim();
    const images = attachments.map(({file}) => file).filter(Boolean);
    const isExample = attachments.length === 1 && attachments[0].url === sample.photo;
    const prices = Array.from(prompt.matchAll(/([0-9][0-9,]*)\s*원/g), (match) => Number(match[1].replaceAll(',', '')));
    if (isExample && prompt === exampleRequest) prices.splice(0, prices.length, ...sample.prices);
    // 모형에서는 요청에 포함될 값을 보관하고 시연용 초안을 표시합니다.
    draft = {
      ...sample,
      ...fields,
      input: {...fields, prompt, images},
      request: prompt,
      sampleArtwork: isExample,
      photos: attachments.map(({url}) => url),
      prices,
      caption: isExample ? sample.caption : '이달의 아트 게시글 예시입니다.',
      history: [],
    };
    attachments.splice(0);
  } else if (type === 'image') {
    draft.prices = form.get('prices').split('\n').map((line) => line.trim()).filter((line) => /^\d[\d,]*$/.test(line)).map((line) => Number(line.replaceAll(',', '')));
    draft.side = form.get('side');
    draft.history = [...draft.history, '이미지의 가격 표시를 수정했어요.'];
  } else {
    for (const key of ['caption', 'tags', 'booking']) draft[key] = form.get(key);
    draft.history = [...draft.history, '게시글 문구를 수정했어요.'];
  }
  location.hash = 'draft';
});

byId('frames').innerHTML = screens.map((screen) => frame(screen, sample, true)).join('');
byId('minus').addEventListener('click', () => setZoom(zoom - .1));
byId('plus').addEventListener('click', () => setZoom(zoom + .1));
byId('fit').addEventListener('click', fit);
window.addEventListener('hashchange', render);
render();
setZoom(Math.min(1, Math.max(.25, byId('viewport').clientWidth / canvasWidth)));
