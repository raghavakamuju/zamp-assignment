async function submitAuth(path, email, password, errorEl) {
  errorEl.textContent = "";
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (res.ok) {
    window.location.href = "/";
    return;
  }
  const body = await res.json().catch(() => ({}));
  errorEl.textContent = body.detail || "Something went wrong.";
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", (e) => {
    e.preventDefault();
    submitAuth(
      "/auth/login",
      document.getElementById("email").value.trim(),
      document.getElementById("password").value,
      document.getElementById("error")
    );
  });
}

const signupForm = document.getElementById("signup-form");
if (signupForm) {
  signupForm.addEventListener("submit", (e) => {
    e.preventDefault();
    submitAuth(
      "/auth/signup",
      document.getElementById("email").value.trim(),
      document.getElementById("password").value,
      document.getElementById("error")
    );
  });
}
