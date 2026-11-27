import { request } from "./api.js";

const dialog = document.createElement("dialog");
dialog.className = "auth-dialog";
dialog.setAttribute("aria-labelledby", "auth-title");
dialog.innerHTML = `
  <form method="dialog">
    <button class="auth-close" type="submit" aria-label="닫기">×</button>
  </form>
  <h2 id="auth-title"></h2>
  <form class="auth-email">
    <label for="auth-email">이메일</label>
    <input id="auth-email" type="email" autocomplete="email" required>
    <div class="auth-email" id="auth-password-field">
      <label for="auth-password">비밀번호</label>
      <input id="auth-password" type="password" required>
    </div>
    <div class="auth-email" id="auth-verification" hidden>
      <label for="auth-code">인증 코드</label>
      <input id="auth-code" type="text" inputmode="numeric" autocomplete="one-time-code"
             pattern="[0-9]{6,10}" maxlength="10" disabled>
      <button class="button button-plain" id="auth-resend" type="button">코드 다시 받기</button>
    </div>
    <button class="button button-primary auth-action" type="submit">인증 코드 받기</button>
  </form>
  <button class="button button-plain" id="auth-switch" type="button"></button>
  <p class="auth-status" role="status"></p>
`;
document.body.append(dialog);

const form = dialog.querySelector(".auth-email");
const email = dialog.querySelector("#auth-email");
const passwordField = dialog.querySelector("#auth-password-field");
const password = dialog.querySelector("#auth-password");
const code = dialog.querySelector("#auth-code");
const verification = dialog.querySelector("#auth-verification");
const status = dialog.querySelector(".auth-status");
const submit = dialog.querySelector(".auth-action");
const switchMode = dialog.querySelector("#auth-switch");
const resend = dialog.querySelector("#auth-resend");
let signup;
let continueToCreate;

// 로그인 또는 회원가입 모드로 인증 팝업을 초기화하고 엽니다.
export function showAuth(mode = "로그인", create = false) {
  if (dialog.open || submit.disabled) return;
  signup = mode === "회원가입";
  continueToCreate = create;
  dialog.querySelector("#auth-title").textContent = mode;
  switchMode.textContent = signup ? "이미 계정이 있으신가요? 로그인" : "계정이 없으신가요? 회원가입";
  form.reset();
  email.readOnly = false;
  passwordField.hidden = false;
  password.disabled = false;
  password.autocomplete = signup ? "new-password" : "current-password";
  password.minLength = signup ? 8 : 0;
  code.disabled = true;
  code.required = false;
  verification.hidden = true;
  submit.textContent = signup ? "가입 인증 코드 받기" : "로그인";
  status.textContent = "";
  dialog.showModal();
  email.focus();
}

// 회원가입 정보를 보내 인증 코드를 요청하고 코드 입력창을 표시합니다.
async function sendSignupCode() {
  await request("/api/auth/signup", "POST", { email: email.value, password: password.value });
  password.value = "";
  password.disabled = true;
  passwordField.hidden = true;
  email.readOnly = true;
  code.disabled = false;
  code.required = true;
  code.value = "";
  verification.hidden = false;
  submit.textContent = "인증하기";
  status.textContent = "이메일로 받은 인증 코드를 입력해 주세요.";
  code.focus();
}

// 인증 요청 중 중복 입력을 막고 오류를 팝업에 표시합니다.
async function runAuth(action) {
  submit.disabled = true;
  resend.disabled = true;
  switchMode.disabled = true;
  status.textContent = "";
  try {
    await action();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
    resend.disabled = false;
    switchMode.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void runAuth(async () => {
    if (!signup) {
      await request("/api/auth/login", "POST", { email: email.value, password: password.value });
      password.value = "";
    } else {
      if (verification.hidden) return sendSignupCode();
      await request("/api/auth/verify", "POST", { email: email.value, code: code.value });
    }
    dialog.close();
    if (continueToCreate) location.assign("./create.html");
    else document.dispatchEvent(new Event("onecut-authenticated"));
  });
});
switchMode.addEventListener("click", () => {
  const address = email.value;
  dialog.close();
  showAuth(signup ? "로그인" : "회원가입", continueToCreate);
  email.value = address;
});
resend.addEventListener("click", () => void runAuth(async () => {
  await request("/api/auth/resend", "POST", { email: email.value });
  status.textContent = "새 인증 코드를 이메일로 보냈습니다.";
  code.value = "";
  code.focus();
}));

document.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-auth], [data-auth-create]");
  if (!trigger || event.defaultPrevented) return;
  event.preventDefault();
  if (!trigger.hasAttribute("data-auth-create")) {
    showAuth(trigger.dataset.auth);
    return;
  }
  try {
    await request("/api/auth/session", "GET");
    location.assign("./create.html");
  } catch (error) {
    showAuth("로그인", true);
    if (error.status !== 401) status.textContent = error.message;
  }
});

document.addEventListener("onecut-auth-required", () => showAuth());
// 인증 성공 후 방문자 메뉴 대신 계정 메뉴를 표시합니다.
function updateAccountActions() {
  document.querySelectorAll("[data-guest]").forEach((node) => { node.hidden = true; });
  document.querySelectorAll("[data-account]").forEach((node) => { node.hidden = false; });
}
document.addEventListener("onecut-authenticated", updateAccountActions);
if (document.querySelector("[data-guest]")) {
  void request("/api/auth/session", "GET").then(updateAccountActions, () => {});
}

const logoutButton = document.querySelector("[data-logout]");
logoutButton?.addEventListener("click", async () => {
  const notice = document.querySelector("[data-logout-status]");
  notice.textContent = "";
  logoutButton.disabled = true;
  try {
    await request("/api/auth/logout", "POST");
    location.replace("./");
  } catch (error) {
    notice.textContent = error.message;
    logoutButton.disabled = false;
  }
});
