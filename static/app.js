// Progressive enhancement only — every action works as a plain form POST
// without this file.

// Close any other open "Covered…" picker when one opens, so long lecture
// lists do not turn into a wall of checkboxes.
document.addEventListener("toggle", (e) => {
  const el = e.target;
  if (!(el instanceof HTMLDetailsElement) || !el.classList.contains("cover")) return;
  if (!el.open) return;
  document.querySelectorAll("details.cover[open]").forEach((other) => {
    if (other !== el) other.open = false;
  });
}, true);

// Auto-dismiss flashes; they are confirmations, not state.
setTimeout(() => {
  document.querySelectorAll(".flash").forEach((f) => {
    f.style.transition = "opacity .4s";
    f.style.opacity = "0";
    setTimeout(() => f.remove(), 400);
  });
}, 4000);
