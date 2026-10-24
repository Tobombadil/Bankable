# Design practices: how Inframation's designers work

**Status:** standing brief for every design and front-end lane, 2026-10-23. Owner: "lean into the rules of that
skill to create a professional design"; "use [spur.us] for reference on best practices ... so they can behave as
a world class designer."

**Order of authority.**
1. The guardrails in `CLAUDE.md`.
2. WCAG 2.2 AA (`docs/04`).
3. The taste skill (`.claude/skills/taste-skill`).
4. This brief.
5. `docs/31-design-system.md`, where it has not yet been brought into line.

This brief adds the practices that a data product's designer needs and the skill does not spell out. It does not
repeat the skill. Read the skill first.

## 1. References and what each is for

| Reference | Why it is on the list | Take | Leave |
|---|---|---|---|
| spur.us (owner, 2026-10-23) | A data-intelligence company the owner holds up as aspirational | To be filled in from the owner's screenshots. The site serves a bot checkpoint to automated browsers, which we do not bypass. | |
| GridStatus (owner: "the closest analogy") | A trusted, data-first view of the grid | Figures first; every chart dated and sourced; dense but calm | |
| Compass (compassitg.com), the parent | Family resemblance | A reference, not a rule: navy, a geometric face, forest, biomass and power imagery, the orange needle as the nod | Its gradient pill button and its floating chat pill (the skill bans both) |
| Cleanview (competitor) | What to stay distinct from | Nothing visual | The cream ground and orange-led palette |

## 2. Practices

Each practice below has its source.

### 2.1 The product is the hero

- **One live thing above the fold.** Find a project, company or place, and see it on the map at once. In the
  skill's blind tests every winning page had one (skill, step 4).
- **Real interface and real data, never illustrations of them.** A cropped panel of the actual map, an actual
  record, an actual figure that adds up (skill, "app screens" §7 and "imagery").
- **Proof is the data, with its provenance on the same screen.** Every figure links to the list that shows it
  (PLAN, 2026-10-23). Every page states when its sources were last read. Trust in data products rests on visible
  provenance and freshness. See NN/g's credibility guidelines,
  https://www.nngroup.com/articles/trustworthy-design/, and Stanford's web credibility guidelines,
  https://credibility.stanford.edu/guidelines/.

### 2.2 Hierarchy and reading

- **People scan.** Front-load headings and labels with the information-carrying words. Users read in an F
  pattern on text-heavy pages. https://www.nngroup.com/articles/f-shaped-pattern-reading-web-content/
- **Fewer choices.** Show the few controls that matter and put the rest behind one "Filters" button. Hick's law:
  decision time grows with the number of options. https://lawsofux.com/hicks-law/
- **Conventions over novelty.** Users spend most of their time on other sites (Jakob's law). Maps, filters,
  search and tables behave as people expect. https://lawsofux.com/jakobs-law/
- **Line length 45 to 75 characters**, body at 16px or more. Bringhurst, *The Elements of Typographic Style*,
  §2.1.2; the skill's laws.

### 2.3 Numbers and charts

- **Choose the encoding by perceptual accuracy:** position on a common scale, then length, then angle and
  area, then colour. Cleveland & McGill (1984), *Graphical Perception*, JASA 79(387). So use bars and lines for
  comparisons, never pies for more than two parts.
- **Data-ink.** Remove gridlines, borders and legends that carry nothing. Label lines directly instead of using
  a legend. Tufte, *The Visual Display of Quantitative Information* (1983), ch. 4.
- **Bars start at zero.** A trend chart says its window, and compares like with like (PLAN: series are never
  spliced).
- **Tabular figures, right-aligned, units in grey, fixed precision.** State the "as of" date with every figure
  (skill, app screens §7.4).
- **Colour is never the only carrier of meaning.** About 8% of men and 0.5% of women have a colour-vision
  deficiency (https://www.nei.nih.gov/learn-about-eye-health/eye-conditions-and-diseases/color-blindness).
  Status also uses shape or glyph (our ring glyphs) and words. WCAG 2.2 SC 1.4.1.
- **Four hues at most on a screen;** one hue per chart (skill, app screens §13).

### 2.4 The map

- **Visual hierarchy by layer.** Projects are the saturated foreground. Existing infrastructure is thin,
  low-contrast context. Detail grows with zoom (PLAN, declutter row). Cartographic practice: Brewer, *Designing
  Better Maps* (2nd ed., 2016), ch. 3 to 5.
- **Clusters show counts and scale by area,** not radius. A radius scale exaggerates differences quadratically.
  Proportional-symbol practice is in Brewer, ch. 5.
- **The key sits next to the data,** not on another screen. Every mark type in the key has words as well as a
  glyph.

### 2.5 Interaction, performance and access

- **Targets at least 24 by 24 CSS px** (WCAG 2.2 SC 2.5.8, AA); 44px for primary touch targets (Apple HIG).
- **Focus is always visible and never hidden by sticky bars or overlays** (SC 2.4.7, 2.4.11).
- **Core Web Vitals at the 75th percentile:** LCP 2.5 s or less, INP 200 ms or less, CLS 0.1 or less
  (https://web.dev/articles/vitals). The map panel's own budget is stricter (docs/30 §11.7, S-8: 0.02).
- **Motion has a job** (feedback, continuity, the one signature move) and a `prefers-reduced-motion` fallback
  (SC 2.3.3; skill, step 9).
- **Every state is designed:** empty, loading (skeletons at final size), error (what happened, what to do), and
  partial data. The skill, app screens §11; docs/31 §6.

### 2.6 Words

- **Labels over sentences.** No description under titles. A standfirst is one line at most. Explanations sit
  behind "Why?" or "About this" (PLAN, declutter row).
- **Buttons say what they do** ("Show 412 projects", not "Submit"). Skill, `references/copy.md`.
- **Plain words for newcomers, precise words for professionals.** The glossary carries the difference, so pages
  can stay short (PLAN, US-first row, item 3).
- **One or two em dashes per page at most** (skill lint).

## 3. How a design lane works

1. **Before.** Run `inspect.mjs` (with `--app` for app screens) and `lint.mjs`, and count words and controls
   above the fold at 1440 and 390.
2. **Run the skill's process.** Use draw, then the idea sentence, then the copy deck, then the build.
   **Explore before converging** (owner, 2026-10-24: "keep being creative and trying new things. I've liked this
   new direction, keep going"): draw at least three distinct directions, one of them bolder than feels safe, and
   show them side by side with a recommendation before building one. The guardrails, WCAG and the honesty rules
   bound the exploration; taste within them is open.
3. **Gate.** No FAIL. Every WARN is fixed or defended in one sentence. Axe shows 0 serious or critical issues.
   CLS is within budget.
4. **Look.** Do three critique rounds (skill `references/critique.md`) on screenshots at 1440, 768 and 390,
   fixing the top three problems each round.
5. **Report** before and after side by side, with every number measured, never estimated.
