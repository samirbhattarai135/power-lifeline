// Landing page motion. Everything here is enhancement: with JS off the page is complete and static.
// The hero pills and the lights-out scroll are CSS. This file arms two one-shot CSS reveals, then,
// after the load event, loads GSAP for the scroll-driven parts (never on the LCP path).

document.documentElement.classList.add("js");

const motionAllowed = () => matchMedia("(prefers-reduced-motion: no-preference)").matches;
// Only things that have not been seen yet may start hidden; anything already on screen stays put.
const notYetSeen = (node) => node.getBoundingClientRect().top > innerHeight;

// ---- one-shot CSS reveals: arm while off-screen, play once in view ------------------------------

function playOnceInView(node, threshold) {
  if (!node || !notYetSeen(node) || !("IntersectionObserver" in window)) return;
  node.classList.add("is-armed");
  const observer = new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) return;
    node.classList.replace("is-armed", "is-in");
    observer.disconnect();
  }, { threshold });
  observer.observe(node);
}

if (motionAllowed()) {
  playOnceInView(document.getElementById("storm-visual"), 0.3);
  if (matchMedia("(max-width: 1023px)").matches) {
    document.querySelectorAll(".step, .tour-row").forEach((node) => playOnceInView(node, 0.2));
  }
}

// ---- GSAP: loaded after the load event, all of it inside one matchMedia ---------------------------

const STEP_CLOCK = ["2:41 AM", "2:41 AM", "2:43 AM", "2:45 AM", "2:47 AM"];
const ALARM_STEP = 3;

// The dist files are UMD and assign self.window, which throws inside an ES module, so they load as
// classic same-origin scripts (allowed by script-src 'self'), in order.
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = false;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.append(script);
  });
}

async function loadGsap() {
  await Promise.all(["gsap", "ScrollTrigger", "SplitText", "DrawSVGPlugin"].map((name) => loadScript(`/static/${name}.min.js`)));
  const { gsap, ScrollTrigger, SplitText, DrawSVGPlugin } = window;
  gsap.registerPlugin(ScrollTrigger, SplitText, DrawSVGPlugin);
  return gsap;
}

function heroParallax(gsap) {
  const photo = document.getElementById("hero-photo");
  const layer = document.getElementById("hero-layer");
  gsap.set(layer, { scale: 1.03 });  // room for ±8 px without showing an edge
  const toX = gsap.quickTo(layer, "x", { duration: 0.6, ease: "power3" });
  const toY = gsap.quickTo(layer, "y", { duration: 0.6, ease: "power3" });
  const move = (event) => {
    const box = photo.getBoundingClientRect();
    toX(((event.clientX - box.left) / box.width - 0.5) * 16);
    toY(((event.clientY - box.top) / box.height - 0.5) * 16);
  };
  const rest = () => { toX(0); toY(0); };
  photo.addEventListener("pointermove", move);
  photo.addEventListener("pointerleave", rest);
  return () => { photo.removeEventListener("pointermove", move); photo.removeEventListener("pointerleave", rest); };
}

function countUp(gsap) {
  const node = document.getElementById("count");
  if (!notYetSeen(node)) return undefined;
  const target = 1897;
  node.style.display = "inline-block";
  node.style.minWidth = `${node.getBoundingClientRect().width}px`;  // no layout shift while it counts
  const counter = { value: 0 };
  node.textContent = "0";
  gsap.to(counter, {
    value: target, duration: 1.2, ease: "power3.out", snap: { value: 1 },
    scrollTrigger: { trigger: node, start: "top 75%", once: true },
    onUpdate: () => { node.textContent = counter.value.toLocaleString("en-US"); },
  });
  return () => { node.textContent = target.toLocaleString("en-US"); };
}

function splitTitles(gsap) {
  const { SplitText } = window;
  for (const title of document.querySelectorAll("h2.split")) {
    if (!notYetSeen(title)) continue;
    SplitText.create(title, {
      type: "lines", mask: "lines", autoSplit: true,
      onSplit: (split) => gsap.from(split.lines, {
        yPercent: 110, duration: 0.8, ease: "expo.out", stagger: 0.08,
        scrollTrigger: { trigger: title, start: "top 85%", once: true },
      }),
    });
  }
}

// One scrubbed timeline, one segment per step; labels sit where each step is complete.
function pinEscalation(gsap) {
  const how = document.getElementById("how");
  const clock = document.getElementById("clock");
  const steps = [...how.querySelectorAll(".step")];
  how.classList.add("is-pinned");
  const showStep = (progress) => {
    // Label N sits where step N is complete, so the clock shows the step being drawn (or just finished).
    const index = Math.max(0, Math.ceil(progress * steps.length - 0.001) - 1);
    clock.textContent = STEP_CLOCK[index];
    clock.toggleAttribute("data-alarm", index === ALARM_STEP);
  };
  const timeline = gsap.timeline({
    defaults: { ease: "none" },
    scrollTrigger: {
      trigger: how,
      start: () => (how.offsetHeight > innerHeight ? "bottom bottom" : "center center"),
      end: "+=300%", pin: true, scrub: 0.5, invalidateOnRefresh: true,
      snap: { snapTo: "labels", duration: { min: 0.2, max: 0.6 }, delay: 0.1, ease: "power1.inOut" },
      onUpdate: (self) => showStep(self.progress),
    },
  });
  timeline.addLabel("start", 0);
  steps.forEach((step, index) => {
    timeline.fromTo(step.querySelector(".rule line"), { drawSVG: "0%" }, { drawSVG: "100%", duration: 1 }, index);
    timeline.fromTo(step.querySelector(".note"), { y: 24, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.3, ease: "power2.out" }, index);
    timeline.addLabel(`step${index + 1}`, index + 1);
  });
  showStep(timeline.scrollTrigger.progress);
  return () => {
    how.classList.remove("is-pinned");
    clock.textContent = STEP_CLOCK[ALARM_STEP];  // the static design state
    clock.setAttribute("data-alarm", "");
  };
}

function drawCallouts(gsap) {
  const stage = document.getElementById("board-stage");
  if (!notYetSeen(stage)) return;
  const marks = stage.querySelectorAll(".mark");
  const labels = stage.querySelectorAll(".callout");
  const timeline = gsap.timeline({ scrollTrigger: { trigger: stage, start: "top 60%", once: true } });
  marks.forEach((mark, index) => {
    // Each callout runs ring, leader, label; the next one starts 150 ms after this leader lands.
    const at = index * 0.9;
    timeline.from(mark.querySelector(".target"), { scale: 0.96, autoAlpha: 0, transformOrigin: "50% 50%", duration: 0.25 }, at);
    timeline.from(mark.querySelector(".leader"), { drawSVG: "0%", duration: 0.5, ease: "power2.out" }, at + 0.25);
    timeline.from(labels[index], { autoAlpha: 0, duration: 0.3 }, at + 0.75);
  });
}

// The tour: each screenshot rises out of a soft clip as it arrives, then the text message follows.
// Only things below the fold start hidden; anything already seen stays put.
function revealTour(gsap) {
  for (const row of document.querySelectorAll(".tour-row")) {
    if (!notYetSeen(row)) continue;
    const shot = row.querySelector(".tour-shot img");
    const copy = row.querySelectorAll(".tour-copy > *");
    const timeline = gsap.timeline({ scrollTrigger: { trigger: row, start: "top 78%", once: true } });
    timeline.from(shot, { clipPath: "inset(14% 6% 14% 6% round 20px)", y: 48, opacity: 0.2, duration: 0.9, ease: "expo.out" });
    timeline.from(copy, { y: 18, autoAlpha: 0, duration: 0.6, ease: "power2.out", stagger: 0.08 }, 0.15);
  }
}

// The board screenshot settles from a slight tilt-back as it scrolls in: scrubbed, never hidden.
function settleBoard(gsap) {
  const shot = document.querySelector(".board-shot");
  gsap.fromTo(shot, { scale: 0.94, y: 40 }, { scale: 1, y: 0, ease: "none",
    scrollTrigger: { trigger: "#board-stage", start: "top bottom", end: "top 35%", scrub: 0.4 } });
}

// The pilot ledger: three lines arrive in order, once.
function revealLedger(gsap) {
  const ledger = document.querySelector(".ledger");
  if (!ledger || !notYetSeen(ledger)) return;
  gsap.from(ledger.querySelectorAll(".line"), { y: 20, autoAlpha: 0, duration: 0.6, ease: "power2.out", stagger: 0.12,
    scrollTrigger: { trigger: ledger, start: "top 80%", once: true } });
}

function magneticButton(gsap) {
  const button = document.getElementById("pilot-cta");
  const toX = gsap.quickTo(button, "x", { duration: 0.3, ease: "power3" });
  const toY = gsap.quickTo(button, "y", { duration: 0.3, ease: "power3" });
  const move = (event) => {
    const box = button.getBoundingClientRect();
    toX(((event.clientX - box.left) / box.width - 0.5) * 12);
    toY(((event.clientY - box.top) / box.height - 0.5) * 12);
  };
  const rest = () => { toX(0); toY(0); };
  button.addEventListener("pointermove", move);
  button.addEventListener("pointerleave", rest);
  return () => { button.removeEventListener("pointermove", move); button.removeEventListener("pointerleave", rest); };
}

async function startMotion() {
  // Reduced motion: the static page is the design. Phones: the motion notes say CSS only. Either way, skip the download.
  if (!motionAllowed() || !matchMedia("(min-width: 1024px)").matches) return;
  let gsap;
  try {
    gsap = await loadGsap();
  } catch (error) {
    console.info("Lifeline: motion library unavailable, the page stays static.", error?.message ?? error);
    return;
  }
  await document.fonts.ready;
  gsap.matchMedia().add({
    motion: "(prefers-reduced-motion: no-preference)",
    desktop: "(min-width: 1024px) and (min-height: 640px)",
    wide: "(min-width: 1100px)",
    finePointer: "(hover: hover) and (pointer: fine)",
  }, (context) => {
    const { motion, desktop, wide, finePointer } = context.conditions;
    if (!motion) return undefined;  // reduced-motion branch: everything already shows its final state
    const cleanups = [];
    cleanups.push(countUp(gsap));
    splitTitles(gsap);
    if (desktop) cleanups.push(pinEscalation(gsap));
    if (wide) drawCallouts(gsap);
    revealTour(gsap);
    settleBoard(gsap);
    revealLedger(gsap);
    if (finePointer && desktop) cleanups.push(heroParallax(gsap));
    if (finePointer) cleanups.push(magneticButton(gsap));
    return () => cleanups.forEach((cleanup) => cleanup?.());
  });
}

const whenIdle = window.requestIdleCallback ?? ((callback) => setTimeout(callback, 200));
if (document.readyState === "complete") whenIdle(startMotion);
else addEventListener("load", () => whenIdle(startMotion), { once: true });
