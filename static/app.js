// Progressive enhancement only — every action works as a plain form POST
// without this file.

// Mark the document so CSS can reveal JS-only affordances.
document.documentElement.classList.add("js");

// Close any other open "Covered" picker when one opens, so long lecture
// lists do not turn into a wall of checkboxes.
document.addEventListener("toggle", (e) => {
  const el = e.target;
  if (!(el instanceof HTMLDetailsElement) || !el.classList.contains("cover")) return;
  if (!el.open) return;
  document.querySelectorAll("details.cover[open]").forEach((other) => {
    if (other !== el) other.open = false;
  });
}, true);

// Keep a row's tint in step when a box is ticked by hand.
document.querySelectorAll("#topic-grid .tpick").forEach((label) => {
  label.addEventListener("change", (e) => {
    const [d, p] = label.querySelectorAll("input[type=checkbox]");
    if (e.target === d && d.checked) p.checked = false;
    if (e.target === p && p.checked) d.checked = false;
    label.classList.toggle("done", d.checked);
    label.classList.toggle("partial", p.checked);
    label.classList.toggle("pending", !d.checked && !p.checked);
  });
});

// Auto-dismiss toast flashes; they are confirmations, not state.
setTimeout(() => {
  document.querySelectorAll(".flashes .flash:not(.static)").forEach((f) => {
    f.style.transition = "opacity .4s, transform .4s";
    f.style.opacity = "0";
    f.style.transform = "translateY(-4px)";
    setTimeout(() => f.remove(), 400);
  });
}, 4000);
