import Constants from 'expo-constants';

/**
 * Base URL of the Trafficator coordinator dashboard server (dashboard.py).
 *
 * The Lane Status screen talks to the same endpoints the web page uses:
 *   - GET /api/geofences  -> lane polygons (sequence + name + polygon)
 *   - GET /api/state      -> live signal state (polled every second)
 *
 * On a phone, `localhost` points at the phone itself, so this must be the
 * LAN IP (or public host) of the machine running the dashboard. Override it
 * without touching code by adding to app.json:
 *
 *   "expo": { "extra": { "apiBaseUrl": "http://192.168.1.50:5001" } }
 */
const FALLBACK_API_BASE_URL = 'http://192.168.1.50:5001';

const extra =
  (Constants.expoConfig?.extra as Record<string, unknown> | undefined) ?? {};

export const API_BASE_URL = (
  (typeof extra.apiBaseUrl === 'string' && extra.apiBaseUrl) ||
  FALLBACK_API_BASE_URL
).replace(/\/$/, '');

/** How often the Lane Status screen polls /api/state, in milliseconds. */
export const STATE_POLL_INTERVAL_MS = 1000;
