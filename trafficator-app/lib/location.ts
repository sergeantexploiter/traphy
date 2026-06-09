import * as Location from 'expo-location';

/** Single-fix GPS timeout — matches the { timeout: 20000 } option in lane-status.html. */
export const GPS_TIMEOUT_MS = 20000;

export type Fix = { lat: number; lon: number };

export function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

/**
 * Ask for permission and take ONE high-accuracy GPS fix (no cache, 20s timeout) —
 * the same single-reading approach the lane-status page uses. Throws on denial or
 * timeout with a user-facing message.
 */
export async function getCurrentFix(): Promise<Fix> {
  const { status } = await Location.requestForegroundPermissionsAsync();
  if (status !== 'granted') {
    throw new Error('Location permission is needed. Enable location access and try again.');
  }
  try {
    const pos = await withTimeout(
      Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High }),
      GPS_TIMEOUT_MS,
    );
    return { lat: pos.coords.latitude, lon: pos.coords.longitude };
  } catch (e) {
    if (e instanceof Error && e.message === 'timeout') {
      throw new Error('Getting your location timed out. Try again with a clear view of the sky.');
    }
    throw e;
  }
}
