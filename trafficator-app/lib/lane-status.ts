/**
 * Pure logic ported from src/static/lane-status.html so the mobile app derives
 * the exact same red / yellow / green signal the web page shows.
 *
 * Flow (same as the web page):
 *   1. Load lane geofences from /api/geofences.
 *   2. Take a single GPS fix and find which lane polygon the user is in.
 *   3. Poll /api/state and translate it into a light + status for that lane.
 */

export type Lamp = { id: string; on: boolean; color: string };

export type PhaseConfig = {
  green_keys?: string[];
  yellow_keys?: string[];
  red_keys?: string[];
};

export type LaneState = {
  current_phase?: string | null;
  phase_sub_state?: string | null;
  standby_mode?: boolean;
  phases?: Record<string, PhaseConfig>;
  lamps?: Lamp[];
  assigned_green_duration?: number;
  manual_green_duration?: number;
  green_elapsed_seconds?: number;
  yellow_elapsed_seconds?: number;
  yellow_duration?: number;
  server_time?: number;
  phase_start_time?: number;
  yellow_start_time?: number;
  error?: string;
};

export type Geofence = {
  sequence: string | null;
  name?: string;
  polygon: [number, number][];
};

export type SignalColor = 'red' | 'yellow' | 'green' | 'neutral';
export type PhaseDot = 'off' | 'green' | 'yellow' | 'red';

export type LaneSignal = {
  /** Which lamp should glow; `null` means all lamps off. */
  lamp: 'red' | 'yellow' | 'green' | null;
  statusText: string;
  statusColor: SignalColor;
  timeRemaining: string;
  phaseDot: PhaseDot;
};

/** Ray-casting point-in-polygon. Polygon points are [lat, lon]. */
export function pointInPolygon(
  lat: number,
  lon: number,
  polygon: [number, number][],
): boolean {
  if (!polygon || polygon.length < 3) return false;
  const x = lon;
  const y = lat;
  let inside = false;
  const n = polygon.length;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = polygon[i][1];
    const yi = polygon[i][0];
    const xj = polygon[j][1];
    const yj = polygon[j][0];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

export function findGeofence(
  lat: number,
  lon: number,
  geofences: Geofence[],
): Geofence | null {
  for (const g of geofences) {
    if (g.polygon && pointInPolygon(lat, lon, g.polygon)) return g;
  }
  return null;
}

export function formatSeqTitle(
  seqKey: string | null | undefined,
  name?: string | null,
): string {
  if (name) return name;
  if (seqKey === 'seq1') return 'Sequence 1';
  if (seqKey === 'seq2') return 'Sequence 2';
  if (seqKey === 'seq3') return 'Sequence 3';
  return seqKey || '—';
}

/** Colour of the small "your sequence" phase dot. */
export function getPhaseDotColor(
  state: LaneState | null,
  seqKey: string | null,
): PhaseDot {
  if (!state || typeof state !== 'object' || !seqKey) return 'off';
  if (state.standby_mode) return 'off';
  if (state.current_phase !== seqKey) return 'off';

  const sub = (state.phase_sub_state || '').toLowerCase();
  if (sub === 'green') return 'green';
  if (sub === 'yellow') return 'yellow';
  if (sub === 'red') return 'red';

  const phases = state.phases || {};
  const phase = phases[seqKey];
  const lamps: Record<string, Lamp> = {};
  (state.lamps || []).forEach((l) => {
    lamps[l.id] = l;
  });

  if (phase) {
    const greenKeys = phase.green_keys || [];
    const yellowKeys = phase.yellow_keys || [];
    const redKeys = phase.red_keys || [];
    if (greenKeys.some((id) => lamps[id] && lamps[id].color === 'green')) return 'green';
    if (yellowKeys.some((id) => lamps[id] && lamps[id].color === 'yellow')) return 'yellow';
    if (redKeys.some((id) => lamps[id] && lamps[id].color === 'red_active')) return 'red';
  }
  return 'off';
}

/**
 * Translate a coordinator state snapshot into the light + status for the
 * user's lane. Mirrors updateLights() in lane-status.html.
 */
export function deriveLaneSignal(
  state: LaneState | null,
  seqKey: string | null,
): LaneSignal {
  if (!seqKey) {
    return {
      lamp: null,
      statusText: 'Not in a monitored lane',
      statusColor: 'neutral',
      timeRemaining: 'Move into an approach zone to see your signal.',
      phaseDot: 'off',
    };
  }

  const s: LaneState = state && typeof state === 'object' ? state : {};
  const isMyPhase = s.current_phase === seqKey;
  const sub = s.phase_sub_state || null;
  const standby = !!s.standby_mode;
  const phaseDot = getPhaseDotColor(s, seqKey);

  if (standby) {
    return {
      lamp: null,
      statusText: 'Lights off (standby)',
      statusColor: 'neutral',
      timeRemaining: '',
      phaseDot,
    };
  }

  if (!isMyPhase || (sub !== 'green' && sub !== 'yellow')) {
    return {
      lamp: 'red',
      statusText: 'Red — wait',
      statusColor: 'red',
      timeRemaining: 'Your lane will turn green in the next cycle.',
      phaseDot,
    };
  }

  if (sub === 'green') {
    const total =
      (s.assigned_green_duration ?? 0) > 0
        ? (s.assigned_green_duration as number)
        : s.manual_green_duration != null
          ? s.manual_green_duration
          : 90;
    const elapsed =
      s.green_elapsed_seconds != null &&
      s.green_elapsed_seconds >= 0 &&
      s.green_elapsed_seconds < 86400
        ? s.green_elapsed_seconds
        : Math.max(0, (s.server_time || Date.now() / 1000) - (s.phase_start_time || 0));
    const left = Math.max(0, total - elapsed);
    return {
      lamp: 'green',
      statusText: 'Green — go',
      statusColor: 'green',
      timeRemaining: total > 0 ? `About ${left.toFixed(0)}s green remaining` : '',
      phaseDot,
    };
  }

  // sub === 'yellow'
  const total = s.yellow_duration || 3;
  const elapsed =
    s.yellow_elapsed_seconds != null &&
    s.yellow_elapsed_seconds >= 0 &&
    s.yellow_elapsed_seconds < 86400
      ? s.yellow_elapsed_seconds
      : Math.max(0, (s.server_time || Date.now() / 1000) - (s.yellow_start_time || 0));
  const left = Math.max(0, total - elapsed);
  return {
    lamp: 'yellow',
    statusText: 'Yellow — clear the intersection',
    statusColor: 'yellow',
    timeRemaining: `About ${left.toFixed(0)}s yellow remaining`,
    phaseDot,
  };
}
