document.addEventListener("click", e => {
  const a = e.target.closest(".sidebar a");
  if (a && window.innerWidth < 800) document.getElementById("sidebar")?.classList.remove("open");
});
