/**
 * Design tokens for the Trafficator app.
 *
 * The visual language is borrowed from the Bitafrika mobile app: the Sora
 * typeface, a brand-blue (#155EEF) accent, white canvas with light system
 * surfaces, and a floating "liquid glass" tab bar.
 *
 * The traffic-signal card on the Lane Status screen intentionally keeps the
 * dark "Tokyo Night" palette of the original `lane-status.html` page so the
 * live signal reads exactly like the web version it mimics.
 */

import { Platform } from 'react-native';

/** Sora weights as loaded by `@expo-google-fonts/sora` (see app/_layout.tsx). */
export const FontFamily = {
  thin: 'Sora_100Thin',
  extraLight: 'Sora_200ExtraLight',
  light: 'Sora_300Light',
  regular: 'Sora_400Regular',
  medium: 'Sora_500Medium',
  semiBold: 'Sora_600SemiBold',
  bold: 'Sora_700Bold',
  extraBold: 'Sora_800ExtraBold',
} as const;

/** Bitafrika-inspired light design system. */
export const Palette = {
  primary: '#155EEF',
  primaryHover: '#1148BD',
  primarySoft: 'rgba(21, 94, 239, 0.12)',
  ink: '#111111',

  canvas: '#ffffff',
  surface: '#f5f5f5',
  surfaceMuted: '#fafafa',

  fgStrong: '#111111',
  fgDefault: '#555555',
  fgMuted: '#666666',
  fgSubtle: '#888888',
  fgFaint: '#9CA3AF',

  line: '#e0e0e0',
  lineSoft: '#f0f0f0',

  danger: '#d93025',
  dangerHover: '#b3271d',
  success: '#1a9c52',

  // Glass tab bar
  glassTintIOS: 'rgba(235, 240, 255, 0.22)',
  glassTintAndroid: 'rgba(240, 243, 255, 0.92)',
  glassBorder: 'rgba(180, 198, 240, 0.55)',
} as const;

/**
 * "Tokyo Night" palette used inside the traffic-signal card so it matches the
 * original lane-status.html UI 1:1.
 */
export const Signal = {
  bg: '#1a1b26',
  card: '#24283b',
  cardInner: '#1e2030',
  border: '#414868',
  text: '#c0caf5',
  textSoft: '#a9b1d6',
  textMuted: '#565f89',
  accent: '#7aa2f7',

  red: '#f7768e',
  yellow: '#e0af68',
  green: '#9ece6a',
  lampOff: '#3b3f5c',
  dotOff: '#3b4261',
} as const;

const tintColorLight = Palette.primary;
const tintColorDark = '#fff';

export const Colors = {
  light: {
    text: '#11181C',
    background: '#fff',
    tint: tintColorLight,
    icon: '#687076',
    tabIconDefault: '#687076',
    tabIconSelected: tintColorLight,
  },
  dark: {
    text: '#ECEDEE',
    background: '#151718',
    tint: tintColorDark,
    icon: '#9BA1A6',
    tabIconDefault: '#9BA1A6',
    tabIconSelected: tintColorDark,
  },
};

export const Fonts = Platform.select({
  ios: {
    sans: 'system-ui',
    serif: 'ui-serif',
    rounded: 'ui-rounded',
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    serif: "Georgia, 'Times New Roman', serif",
    rounded: "'SF Pro Rounded', 'Hiragino Maru Gothic ProN', Meiryo, 'MS PGothic', sans-serif",
    mono: "SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
  },
});
