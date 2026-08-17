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

// "Done till lecture N" -> re-tick the topics that N implies. The server owns
// the mapping (it has the handout's lecture numbers); this only paints it.
// Without JS the form still works — you just tick the boxes yourself.
const watermark = document.querySelector('input[name="lectures_done"][data-suggest-url]');
if (watermark) {
  let timer = null;
  const apply = async () => {
    const n = parseInt(watermark.value, 10);
    if (Number.isNaN(n)) return;
    let data;
    try {
      const res = await fetch(`${watermark.dataset.suggestUrl}?n=${n}`,
                              { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      data = await res.json();
    } catch { return; }

    const done = new Set(data.done), partial = new Set(data.partial);
    const unnumbered = new Set(data.unnumbered);
    document.querySelectorAll("#topic-grid .tpick").forEach((label) => {
      const [d, p] = label.querySelectorAll("input[type=checkbox]");
      const id = parseInt(d.value, 10);
      // A topic the handout never numbered is the owner's call, so leave
      // whatever they ticked rather than clearing it on every keystroke.
      if (unnumbered.has(id)) return;
      d.checked = done.has(id);
      p.checked = partial.has(id);
      label.classList.toggle("done", d.checked);
      label.classList.toggle("partial", p.checked);
      label.classList.toggle("pending", !d.checked && !p.checked);
    });
  };
  watermark.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(apply, 250);
  });
}

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

// Auto-dismiss flashes; they are confirmations, not state.
setTimeout(() => {
  document.querySelectorAll(".flash").forEach((f) => {
    f.style.transition = "opacity .4s";
    f.style.opacity = "0";
    setTimeout(() => f.remove(), 400);
  });
}, 4000);
