/**
 * Material 3 动态取色。
 *
 * Kazumi 用 Flutter 的 ColorScheme.fromSeed（内部就是 material-color-utilities
 * 的 SchemeTonalSpot）。这里直接使用同一份官方库，所以九种预设主题色算出来的
 * 色板与 Kazumi 完全一致，而不是"看着差不多"的近似值。
 */
import { Hct, SchemeTonalSpot, argbFromHex, hexFromArgb } from '../vendor/material-color-utilities.js';

/** M3 的全部颜色角色 —— CSS 变量名就是 "md-" + kebab-case。 */
const ROLES = [
  'primary', 'onPrimary', 'primaryContainer', 'onPrimaryContainer',
  'secondary', 'onSecondary', 'secondaryContainer', 'onSecondaryContainer',
  'tertiary', 'onTertiary', 'tertiaryContainer', 'onTertiaryContainer',
  'error', 'onError', 'errorContainer', 'onErrorContainer',
  'background', 'onBackground', 'surface', 'onSurface',
  'surfaceVariant', 'onSurfaceVariant', 'outline', 'outlineVariant',
  'inverseSurface', 'inverseOnSurface', 'inversePrimary',
  'surfaceDim', 'surfaceBright',
  'surfaceContainerLowest', 'surfaceContainerLow', 'surfaceContainer',
  'surfaceContainerHigh', 'surfaceContainerHighest',
  'shadow', 'scrim',
];

const kebab = (name) => name.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());

/** 手动实现一份降级色板：万一官方库加载失败，界面也不会变成一片黑白。 */
function fallbackScheme(seedHex, dark) {
  const toRgb = (hex) => {
    const n = parseInt(hex.replace('#', ''), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const [r, g, b] = toRgb(seedHex);
  const mix = (c, target, amount) => Math.round(c + (target - c) * amount);
  const tone = (amount) => {
    const t = dark ? 0 : 255;
    return '#' + [r, g, b].map((c) => mix(c, t, amount).toString(16).padStart(2, '0')).join('');
  };
  return {
    primary: dark ? tone(-0.35) : tone(0.1),
    onPrimary: dark ? '#000' : '#fff',
    primaryContainer: dark ? tone(-0.72) : tone(0.72),
    onPrimaryContainer: dark ? tone(0.75) : tone(-0.72),
    surface: dark ? '#141414' : '#fafafa',
    onSurface: dark ? '#e6e6e6' : '#1b1b1b',
    surfaceVariant: dark ? '#444' : '#e2e2e2',
    onSurfaceVariant: dark ? '#c4c4c4' : '#444',
    outline: dark ? '#8f8f8f' : '#757575',
    outlineVariant: dark ? '#444' : '#cacaca',
    surfaceContainerLow: dark ? '#1c1c1c' : '#f4f4f4',
    surfaceContainer: dark ? '#1f1f1f' : '#efefef',
    surfaceContainerHigh: dark ? '#282828' : '#e9e9e9',
    surfaceContainerHighest: dark ? '#303030' : '#e2e2e2',
    surfaceContainerLowest: dark ? '#0d0d0d' : '#ffffff',
    secondaryContainer: dark ? tone(-0.6) : tone(0.6),
    onSecondaryContainer: dark ? tone(0.7) : tone(-0.65),
    inverseSurface: dark ? '#e6e6e6' : '#303030',
    inverseOnSurface: dark ? '#1b1b1b' : '#f4f4f4',
  };
}

export function prefersDark() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function resolveDark(mode) {
  if (mode === 'oled' || mode === 'dark') return true;
  if (mode === 'light') return false;
  return prefersDark();
}

/**
 * 把主题应用到 :root 上的 CSS 变量。
 * @returns {{dark: boolean, scheme: object|null}}
 */
export function applyTheme({ seed = '#4CAF50', mode = 'dark', dynamic = true } = {}) {
  const dark = resolveDark(mode);
  let scheme = null;

  if (dynamic) {
    try {
      scheme = new SchemeTonalSpot(Hct.fromInt(argbFromHex(seed)), dark, 0.0);
    } catch (error) {
      console.warn('[theme] 动态取色不可用，改用降级色板：', error);
      scheme = null;
    }
  }

  const root = document.documentElement;
  const fallback = fallbackScheme(seed, dark);

  for (const role of ROLES) {
    let value = null;
    if (scheme) {
      try {
        const argb = scheme[role];
        if (typeof argb === 'number') value = hexFromArgb(argb);
      } catch { /* 该角色缺失时走降级 */ }
    }
    if (!value) value = fallback[role] || (dark ? '#1f1f1f' : '#efefef');
    root.style.setProperty('--md-' + kebab(role), value);
  }
  // 降级色板里多出来的两个角色
  for (const [name, value] of Object.entries(fallback)) {
    const key = '--md-' + kebab(name);
    if (!root.style.getPropertyValue(key)) root.style.setProperty(key, value);
  }

  document.body.classList.toggle('mode-oled', mode === 'oled');
  document.body.classList.toggle('mode-dark', dark && mode !== 'oled');
  document.body.classList.toggle('mode-light', !dark);
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';

  return { dark, scheme };
}

/** 取当前主色，供图表 / 内联样式使用。 */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export const THEME_MODES = [
  { id: 'system', label: '跟随系统' },
  { id: 'light', label: '浅色' },
  { id: 'dark', label: '深色' },
  { id: 'oled', label: 'OLED 纯黑' },
];
