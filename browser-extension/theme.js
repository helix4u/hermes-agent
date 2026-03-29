const DEFAULT_THEME_ID = "obsidian";
const DEFAULT_CUSTOM_THEME_PRIMARY = "#8b5cf6";
const DEFAULT_CUSTOM_THEME_SECONDARY = "#22d3ee";

const LEGACY_THEME_ALIASES = {
  custom: "obsidian",
  sepiatone: "original"
};

const MONO_DARK_THEME_SPECS = [
  {
    id: "obsidian",
    label: "Obsidian",
    description: "Pitch-black glass with white ink and cool silver edges.",
    bodyStart: "#0a0a0b",
    bodyEnd: "#050506",
    glowTop: "#e5e7eb",
    glowBottom: "#94a3b8",
    accent: "#e5e7eb",
    accentStrong: "#9ca3af",
    textMain: "#f5f5f5",
    textSoft: "#d4d4d8",
    textMuted: "#a1a1aa",
    textFaint: "#71717a",
    card: "#111215",
    field: "#0a0b0d"
  },
  {
    id: "slate",
    label: "Slate",
    description: "Muted controller-gray panels with a steel HUD finish.",
    bodyStart: "#101418",
    bodyEnd: "#080b0f",
    glowTop: "#cbd5e1",
    glowBottom: "#64748b",
    accent: "#cbd5e1",
    accentStrong: "#64748b",
    textMain: "#f8fafc",
    textSoft: "#dbe4ee",
    textMuted: "#94a3b8",
    textFaint: "#64748b",
    card: "#151b21",
    field: "#0c1117"
  },
  {
    id: "graphite",
    label: "Graphite",
    description: "Soft charcoal gradients with pencil-white typography.",
    bodyStart: "#141414",
    bodyEnd: "#0a0a0a",
    glowTop: "#fafaf9",
    glowBottom: "#a8a29e",
    accent: "#e7e5e4",
    accentStrong: "#a8a29e",
    textMain: "#fafaf9",
    textSoft: "#e7e5e4",
    textMuted: "#a8a29e",
    textFaint: "#78716c",
    card: "#1b1a19",
    field: "#12100f"
  },
  {
    id: "terminal",
    label: "Terminal",
    description: "Retro command-center monochrome with razor-sharp contrast.",
    bodyStart: "#090909",
    bodyEnd: "#020202",
    glowTop: "#ffffff",
    glowBottom: "#71717a",
    accent: "#ffffff",
    accentStrong: "#d4d4d8",
    textMain: "#fafafa",
    textSoft: "#e4e4e7",
    textMuted: "#a1a1aa",
    textFaint: "#71717a",
    card: "#0f1011",
    field: "#060708"
  }
];

const LIGHT_THEME_SPECS = [
  {
    id: "newsprint",
    label: "Newsprint",
    description: "Crisp paper-white surfaces with editorial ink contrast.",
    bodyStart: "#fafaf9",
    bodyEnd: "#ecebea",
    glowTop: "#d6d3d1",
    glowBottom: "#94a3b8",
    accent: "#111827",
    accentStrong: "#475569",
    textMain: "#111827",
    textSoft: "#1f2937",
    textMuted: "#475569",
    textFaint: "#64748b",
    card: "#ffffff",
    field: "#ffffff"
  },
  {
    id: "silver",
    label: "Silver",
    description: "Brushed silver UI with sharper chrome contrast.",
    bodyStart: "#f3f4f6",
    bodyEnd: "#d1d5db",
    glowTop: "#ffffff",
    glowBottom: "#9ca3af",
    accent: "#111827",
    accentStrong: "#6b7280",
    textMain: "#111827",
    textSoft: "#374151",
    textMuted: "#4b5563",
    textFaint: "#6b7280",
    card: "#f9fafb",
    field: "#ffffff"
  }
];

const CLASSIC_THEME_SPECS = [
  {
    id: "original",
    label: "Original",
    description: "Warm brass, ember noir, and the old Hermes command glow.",
    bodyStart: "#0f0d0a",
    bodyEnd: "#17120d",
    glowTop: "#ffbf49",
    glowBottom: "#d67531",
    accent: "#d4a75d",
    accentStrong: "#f1b44c",
    textMain: "#f8f0dc",
    textSoft: "#dbc8a3",
    textMuted: "#cdb58d",
    textFaint: "#bfa47a",
    card: "#1b150f",
    field: "#070707"
  }
];

const RETRO_THEME_SPECS = [
  {
    id: "retro-cabinet",
    label: "Retro Console",
    description: "Old-console beige plastics, phosphor green text, and dusty cartridge-era restraint.",
    bodyStart: "#12130d",
    bodyEnd: "#1d1a12",
    glowTop: "#d6ccb0",
    glowBottom: "#7f9461",
    accent: "#cfc6a2",
    accentStrong: "#7f9461",
    textMain: "#e7f2c9",
    textSoft: "#d3e0b6",
    textMuted: "#a8b68a",
    textFaint: "#7f8e63",
    card: "#1c1a13",
    field: "#12110d"
  }
];

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function normalizeHexColor(value, fallback = DEFAULT_CUSTOM_THEME_PRIMARY) {
  const raw = String(value || "").trim();
  if (/^#?[0-9a-fA-F]{6}$/.test(raw)) {
    return `#${raw.replace(/^#/, "").toLowerCase()}`;
  }
  if (/^#?[0-9a-fA-F]{3}$/.test(raw)) {
    const short = raw.replace(/^#/, "").toLowerCase();
    return `#${short.split("").map((char) => `${char}${char}`).join("")}`;
  }
  return fallback;
}

function hexToRgb(hex) {
  const normalized = normalizeHexColor(hex);
  const value = normalized.slice(1);
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16)
  };
}

function rgbToHex({ r, g, b }) {
  return `#${[r, g, b]
    .map((channel) => clamp(Math.round(channel), 0, 255).toString(16).padStart(2, "0"))
    .join("")}`;
}

function rgbToHsl({ r, g, b }) {
  const red = r / 255;
  const green = g / 255;
  const blue = b / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const lightness = (max + min) / 2;

  if (max === min) {
    return { h: 0, s: 0, l: lightness };
  }

  const delta = max - min;
  const saturation =
    lightness > 0.5 ? delta / (2 - max - min) : delta / (max + min);

  let hue = 0;
  switch (max) {
    case red:
      hue = (green - blue) / delta + (green < blue ? 6 : 0);
      break;
    case green:
      hue = (blue - red) / delta + 2;
      break;
    default:
      hue = (red - green) / delta + 4;
      break;
  }

  return { h: hue / 6, s: saturation, l: lightness };
}

function hslToRgb({ h, s, l }) {
  if (s === 0) {
    const channel = Math.round(l * 255);
    return { r: channel, g: channel, b: channel };
  }

  const hueToRgb = (p, q, t) => {
    let next = t;
    if (next < 0) next += 1;
    if (next > 1) next -= 1;
    if (next < 1 / 6) return p + (q - p) * 6 * next;
    if (next < 1 / 2) return q;
    if (next < 2 / 3) return p + (q - p) * (2 / 3 - next) * 6;
    return p;
  };

  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;

  return {
    r: Math.round(hueToRgb(p, q, h + 1 / 3) * 255),
    g: Math.round(hueToRgb(p, q, h) * 255),
    b: Math.round(hueToRgb(p, q, h - 1 / 3) * 255)
  };
}

function shiftHexColor(hex, { hueShift = 0, saturationScale = 1, lightnessDelta = 0 } = {}) {
  const hsl = rgbToHsl(hexToRgb(hex));
  return rgbToHex(
    hslToRgb({
      h: (hsl.h + hueShift / 360 + 1) % 1,
      s: clamp(hsl.s * saturationScale, 0, 1),
      l: clamp(hsl.l + lightnessDelta, 0, 1)
    })
  );
}

function withAlpha(hex, alpha) {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${clamp(alpha, 0, 1)})`;
}

function buildDarkPalette(spec) {
  return {
    bodyBg: `radial-gradient(circle at top right, ${withAlpha(spec.glowTop, 0.16)}, transparent 34%), radial-gradient(circle at bottom left, ${withAlpha(spec.glowBottom, 0.12)}, transparent 28%), linear-gradient(180deg, ${spec.bodyStart} 0%, ${spec.bodyEnd} 100%)`,
    textMain: spec.textMain,
    textSoft: spec.textSoft,
    textMuted: spec.textMuted,
    textFaint: spec.textFaint,
    textAccent: "#ffffff",
    accent: spec.accent,
    accentStrong: spec.accentStrong,
    accentContrast: "#050506",
    accentGlow: withAlpha(spec.accent, 0.18),
    accentGhost: withAlpha(spec.accent, 0.08),
    cardBg: withAlpha(spec.card, 0.9),
    cardBorder: withAlpha(spec.accent, 0.14),
    cardInset: "rgba(255, 255, 255, 0.05)",
    fieldBg: withAlpha(spec.field, 0.74),
    fieldBorder: withAlpha(spec.accent, 0.16),
    fieldFocus: withAlpha(spec.accentStrong, 0.58),
    chipBg: withAlpha(spec.accent, 0.05),
    chipBorder: withAlpha(spec.accent, 0.1),
    subtleBorder: withAlpha(spec.accent, 0.08),
    subtleBg: withAlpha(spec.accent, 0.04),
    secondaryBg: "rgba(255, 255, 255, 0.035)",
    userBubbleBg: `linear-gradient(135deg, ${withAlpha(spec.accent, 0.12)}, ${withAlpha(spec.accentStrong, 0.22)})`,
    userBubbleBorder: withAlpha(spec.accentStrong, 0.22),
    primaryButtonBg: `linear-gradient(135deg, ${spec.accent}, ${spec.accentStrong})`,
    ghostButtonBg: withAlpha(spec.accent, 0.08),
    dangerBg: "rgba(127, 29, 29, 0.2)",
    dangerText: "#fee2e2"
  };
}

function buildLightPalette(spec) {
  return {
    bodyBg: `radial-gradient(circle at top right, ${withAlpha(spec.glowTop, 0.18)}, transparent 34%), radial-gradient(circle at bottom left, ${withAlpha(spec.glowBottom, 0.14)}, transparent 28%), linear-gradient(180deg, ${spec.bodyStart} 0%, ${spec.bodyEnd} 100%)`,
    textMain: spec.textMain,
    textSoft: spec.textSoft,
    textMuted: spec.textMuted,
    textFaint: spec.textFaint,
    textAccent: "#000000",
    accent: spec.accent,
    accentStrong: spec.accentStrong,
    accentContrast: "#ffffff",
    accentGlow: withAlpha(spec.accentStrong, 0.18),
    accentGhost: withAlpha(spec.accentStrong, 0.08),
    cardBg: withAlpha(spec.card, 0.92),
    cardBorder: withAlpha(spec.accentStrong, 0.18),
    cardInset: withAlpha("#ffffff", 0.8),
    fieldBg: withAlpha(spec.field, 0.9),
    fieldBorder: withAlpha(spec.accentStrong, 0.18),
    fieldFocus: withAlpha(spec.accentStrong, 0.34),
    chipBg: withAlpha(spec.accentStrong, 0.06),
    chipBorder: withAlpha(spec.accentStrong, 0.12),
    subtleBorder: withAlpha(spec.accentStrong, 0.1),
    subtleBg: withAlpha(spec.accentStrong, 0.05),
    secondaryBg: withAlpha(spec.accentStrong, 0.035),
    userBubbleBg: `linear-gradient(135deg, ${withAlpha(spec.accentStrong, 0.12)}, ${withAlpha(spec.accentStrong, 0.2)})`,
    userBubbleBorder: withAlpha(spec.accentStrong, 0.18),
    primaryButtonBg: `linear-gradient(135deg, ${spec.accent}, ${shiftHexColor(spec.accent, { lightnessDelta: 0.12 })})`,
    ghostButtonBg: withAlpha(spec.accentStrong, 0.08),
    dangerBg: "rgba(220, 38, 38, 0.12)",
    dangerText: "#7f1d1d"
  };
}

function normalizeCustomThemeDefinition(theme = {}, index = 0) {
  const label = String(theme.label || theme.name || "").trim() || `Custom Theme ${index + 1}`;
  const mode = String(theme.mode || "").trim().toLowerCase() === "light" ? "light" : "dark";
  const primaryFallback = mode === "light" ? "#111827" : DEFAULT_CUSTOM_THEME_PRIMARY;
  const secondaryFallback = mode === "light" ? "#64748b" : DEFAULT_CUSTOM_THEME_SECONDARY;
  const textFallback = mode === "light" ? "#111827" : "#f8fafc";
  const mutedTextFallback = mode === "light" ? "#475569" : "#94a3b8";
  const surfaceFallback = mode === "light" ? "#ffffff" : "#1b1a25";
  const fieldFallback = mode === "light" ? "#ffffff" : "#11131d";
  const fieldTextFallback = mode === "light" ? "#111827" : "#f8fafc";
  const primaryColor = normalizeHexColor(theme.primaryColor || theme.accent || "", primaryFallback);
  const secondaryColor = normalizeHexColor(theme.secondaryColor || theme.accentStrong || "", secondaryFallback);
  return {
    id: String(theme.id || "").trim() || `custom-${slugify(label) || index + 1}`,
    label,
    mode,
    primaryColor,
    secondaryColor,
    textColor: normalizeHexColor(theme.textColor || "", textFallback),
    mutedTextColor: normalizeHexColor(theme.mutedTextColor || "", mutedTextFallback),
    surfaceColor: normalizeHexColor(theme.surfaceColor || "", surfaceFallback),
    fieldColor: normalizeHexColor(theme.fieldColor || "", fieldFallback),
    fieldTextColor: normalizeHexColor(theme.fieldTextColor || "", fieldTextFallback)
  };
}

function normalizeCustomThemes(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  const usedIds = new Set();
  const normalized = [];
  value.forEach((theme, index) => {
    if (!theme || typeof theme !== "object") {
      return;
    }
    const next = normalizeCustomThemeDefinition(theme, index);
    let candidateId = next.id;
    let suffix = 2;
    while (usedIds.has(candidateId)) {
      candidateId = `${next.id}-${suffix}`;
      suffix += 1;
    }
    usedIds.add(candidateId);
    normalized.push({
      ...next,
      id: candidateId
    });
  });
  return normalized;
}

function buildCustomPalette(theme) {
  const primary = normalizeHexColor(theme.primaryColor, DEFAULT_CUSTOM_THEME_PRIMARY);
  const secondary = normalizeHexColor(theme.secondaryColor, DEFAULT_CUSTOM_THEME_SECONDARY);
  const mode = theme.mode === "light" ? "light" : "dark";
  const textColor = normalizeHexColor(theme.textColor, mode === "light" ? "#111827" : "#f8fafc");
  const mutedTextColor = normalizeHexColor(theme.mutedTextColor, mode === "light" ? "#475569" : "#94a3b8");
  const surfaceColor = normalizeHexColor(theme.surfaceColor, mode === "light" ? "#ffffff" : "#1b1a25");
  const fieldColor = normalizeHexColor(theme.fieldColor, mode === "light" ? "#ffffff" : "#11131d");
  const fieldTextColor = normalizeHexColor(theme.fieldTextColor, mode === "light" ? "#111827" : "#f8fafc");

  if (mode === "light") {
    const primarySoft = shiftHexColor(primary, { saturationScale: 0.82, lightnessDelta: 0.1 });
    const secondarySoft = shiftHexColor(secondary, { saturationScale: 0.82, lightnessDelta: 0.12 });
    return {
      bodyBg: `radial-gradient(circle at top right, ${withAlpha(primarySoft, 0.18)}, transparent 34%), radial-gradient(circle at bottom left, ${withAlpha(secondarySoft, 0.16)}, transparent 28%), linear-gradient(180deg, #ffffff 0%, #eef2f7 100%)`,
      textMain: textColor,
      textSoft: shiftHexColor(textColor, { saturationScale: 0.82, lightnessDelta: 0.04 }),
      textMuted: mutedTextColor,
      textFaint: shiftHexColor(mutedTextColor, { saturationScale: 0.88, lightnessDelta: 0.08 }),
      textAccent: textColor,
      accent: primary,
      accentStrong: secondary,
      accentContrast: "#ffffff",
      accentGlow: withAlpha(primary, 0.2),
      accentGhost: withAlpha(primary, 0.08),
      cardBg: withAlpha(surfaceColor, 0.94),
      cardBorder: withAlpha(primary, 0.18),
      cardInset: "rgba(255, 255, 255, 0.82)",
      fieldBg: withAlpha(fieldColor, 0.92),
      fieldBorder: withAlpha(primary, 0.18),
      fieldFocus: withAlpha(secondary, 0.34),
      fieldText: fieldTextColor,
      chipBg: withAlpha(primary, 0.06),
      chipBorder: withAlpha(primary, 0.12),
      subtleBorder: withAlpha(primary, 0.1),
      subtleBg: withAlpha(primary, 0.05),
      secondaryBg: withAlpha(secondary, 0.035),
      userBubbleBg: `linear-gradient(135deg, ${withAlpha(primary, 0.12)}, ${withAlpha(secondary, 0.2)})`,
      userBubbleBorder: withAlpha(secondary, 0.18),
      primaryButtonBg: `linear-gradient(135deg, ${primary}, ${secondary})`,
      ghostButtonBg: withAlpha(primary, 0.08),
      dangerBg: "rgba(220, 38, 38, 0.12)",
      dangerText: "#7f1d1d"
    };
  }

  const primarySoft = shiftHexColor(primary, { saturationScale: 0.88, lightnessDelta: 0.16 });
  const secondarySoft = shiftHexColor(secondary, { saturationScale: 0.88, lightnessDelta: 0.1 });
  const surface = surfaceColor;
  const field = fieldColor;
  return {
    bodyBg: `radial-gradient(circle at top right, ${withAlpha(primarySoft, 0.18)}, transparent 34%), radial-gradient(circle at bottom left, ${withAlpha(secondarySoft, 0.14)}, transparent 28%), linear-gradient(180deg, #090a12 0%, #11131d 100%)`,
    textMain: textColor,
    textSoft: withAlpha(textColor, 0.92),
    textMuted: mutedTextColor,
    textFaint: withAlpha(mutedTextColor, 0.74),
    textAccent: textColor,
    accent: primarySoft,
    accentStrong: secondarySoft,
    accentContrast: "#050506",
    accentGlow: withAlpha(primary, 0.2),
    accentGhost: withAlpha(primarySoft, 0.08),
    cardBg: withAlpha(surface, 0.9),
    cardBorder: withAlpha(primarySoft, 0.18),
    cardInset: "rgba(255, 255, 255, 0.05)",
    fieldBg: withAlpha(field, 0.74),
    fieldBorder: withAlpha(primarySoft, 0.16),
    fieldFocus: withAlpha(secondarySoft, 0.58),
    fieldText: fieldTextColor,
    chipBg: withAlpha(primarySoft, 0.05),
    chipBorder: withAlpha(primarySoft, 0.1),
    subtleBorder: withAlpha(primarySoft, 0.08),
    subtleBg: withAlpha(primarySoft, 0.04),
    secondaryBg: "rgba(255, 255, 255, 0.035)",
    userBubbleBg: `linear-gradient(135deg, ${withAlpha(primarySoft, 0.12)}, ${withAlpha(secondarySoft, 0.22)})`,
    userBubbleBorder: withAlpha(secondarySoft, 0.22),
    primaryButtonBg: `linear-gradient(135deg, ${primarySoft}, ${secondarySoft})`,
    ghostButtonBg: withAlpha(primarySoft, 0.08),
    dangerBg: "rgba(127, 29, 29, 0.2)",
    dangerText: "#fee2e2"
  };
}

function buildCustomThemeEntries(settings = {}) {
  const customThemes = normalizeCustomThemes(settings.customThemes);
  return customThemes.map((theme) => ({
    id: theme.id,
    label: theme.label,
    description: "Custom theme built from your named colors, text tones, and surface styling.",
    mode: theme.mode,
    group: "Custom themes",
    accent: theme.primaryColor,
    palette: buildCustomPalette(theme)
  }));
}

function buildThemeEntries(specs, { mode, group, builder }) {
  return specs.map((spec) => ({
    id: spec.id,
    label: spec.label,
    description: spec.description,
    mode,
    group,
    accent: spec.accent,
    palette: builder(spec)
  }));
}

const BUILT_IN_THEME_ENTRIES = [
  ...buildThemeEntries(MONO_DARK_THEME_SPECS, {
    mode: "dark",
    group: "Monochrome dark",
    builder: buildDarkPalette
  }),
  ...buildThemeEntries(LIGHT_THEME_SPECS, {
    mode: "light",
    group: "Light themes",
    builder: buildLightPalette
  }),
  ...buildThemeEntries(CLASSIC_THEME_SPECS, {
    mode: "dark",
    group: "Original",
    builder: buildDarkPalette
  }),
  ...buildThemeEntries(RETRO_THEME_SPECS, {
    mode: "dark",
    group: "Retro",
    builder: buildDarkPalette
  })
];

function getThemeEntries(settings = {}) {
  return [...BUILT_IN_THEME_ENTRIES, ...buildCustomThemeEntries(settings)];
}

function getThemePresetEntries(settings = {}) {
  return getThemeEntries(settings).map((preset) => ({
    id: preset.id,
    label: preset.label,
    description: preset.description,
    mode: preset.mode,
    group: preset.group,
    accent: preset.accent
  }));
}

function normalizeThemeName(themeName, settings = {}) {
  const raw = String(themeName || "").trim().toLowerCase();
  const entries = getThemeEntries(settings);
  const byId = new Set(entries.map((entry) => entry.id));
  if (!raw) {
    return DEFAULT_THEME_ID;
  }
  if (byId.has(raw)) {
    return raw;
  }
  const aliased = LEGACY_THEME_ALIASES[raw];
  if (aliased && byId.has(aliased)) {
    return aliased;
  }
  return DEFAULT_THEME_ID;
}

function normalizeThemeSettings(settings = {}) {
  const customThemes = normalizeCustomThemes(settings.customThemes);
  return {
    themeName: normalizeThemeName(settings.themeName, { customThemes }),
    customThemeAccent: normalizeHexColor(settings.customThemeAccent || "", DEFAULT_CUSTOM_THEME_PRIMARY),
    customThemes
  };
}

function resolveThemePalette(settings = {}) {
  const normalized = normalizeThemeSettings(settings);
  const preset =
    getThemeEntries(normalized).find((entry) => entry.id === normalized.themeName) ||
    BUILT_IN_THEME_ENTRIES.find((entry) => entry.id === DEFAULT_THEME_ID);
  return {
    id: preset.id,
    label: preset.label,
    description: preset.description,
    mode: preset.mode,
    group: preset.group,
    accent: preset.accent,
    palette: preset.palette
  };
}

function applyThemeToDocument(settings = {}, doc = document) {
  if (!doc?.documentElement) {
    return null;
  }
  const resolved = resolveThemePalette(settings);
  const root = doc.documentElement;
  const style = root.style;
  const palette = resolved.palette;
  const fieldPlaceholder = withAlpha(
    palette.fieldText || palette.textMain || "#f5f5f5",
    0.68
  );
  const cssVars = {
    "--theme-body-bg": palette.bodyBg,
    "--theme-text-main": palette.textMain,
    "--theme-text-soft": palette.textSoft,
    "--theme-text-muted": palette.textMuted,
    "--theme-text-faint": palette.textFaint,
    "--theme-text-accent": palette.textAccent,
    "--theme-accent": palette.accent,
    "--theme-accent-strong": palette.accentStrong,
    "--theme-accent-contrast": palette.accentContrast,
    "--theme-accent-glow": palette.accentGlow,
    "--theme-accent-ghost": palette.accentGhost,
    "--theme-card-bg": palette.cardBg,
    "--theme-card-border": palette.cardBorder,
    "--theme-card-inset": palette.cardInset,
    "--theme-field-bg": palette.fieldBg,
    "--theme-field-border": palette.fieldBorder,
    "--theme-field-focus": palette.fieldFocus,
    "--theme-field-text": palette.fieldText || palette.textMain,
    "--theme-field-placeholder": fieldPlaceholder,
    "--theme-chip-bg": palette.chipBg,
    "--theme-chip-border": palette.chipBorder,
    "--theme-subtle-border": palette.subtleBorder,
    "--theme-subtle-bg": palette.subtleBg,
    "--theme-secondary-bg": palette.secondaryBg,
    "--theme-user-bubble-bg": palette.userBubbleBg,
    "--theme-user-bubble-border": palette.userBubbleBorder,
    "--theme-primary-button-bg": palette.primaryButtonBg,
    "--theme-ghost-button-bg": palette.ghostButtonBg,
    "--theme-danger-bg": palette.dangerBg,
    "--theme-danger-text": palette.dangerText
  };
  Object.entries(cssVars).forEach(([name, value]) => style.setProperty(name, value));
  root.dataset.themeName = resolved.id;
  root.dataset.themeMode = resolved.mode;
  root.dataset.themeGroup = resolved.group;
  root.style.colorScheme = resolved.mode;
  return resolved;
}

window.HermesTheme = {
  defaultThemeId: DEFAULT_THEME_ID,
  defaultCustomThemePrimary: DEFAULT_CUSTOM_THEME_PRIMARY,
  defaultCustomThemeSecondary: DEFAULT_CUSTOM_THEME_SECONDARY,
  getThemePresetEntries,
  normalizeThemeSettings,
  normalizeCustomThemeDefinition,
  normalizeCustomThemes,
  resolveThemePalette,
  applyThemeToDocument,
  normalizeHexColor
};
