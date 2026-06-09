import * as Location from "expo-location";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { API_BASE_URL, STATE_POLL_INTERVAL_MS } from "@/constants/config";
import { FontFamily, Palette, Signal } from "@/constants/theme";
import { deriveLaneSignal, findGeofence, formatSeqTitle, type Geofence, type LaneState, type PhaseDot, type SignalColor } from "@/lib/lane-status";

type Screen = "loading" | "locating" | "ready" | "error";
type CurrentSeq = { sequence: string; name?: string };

const DOT_COLORS: Record<PhaseDot, string> = {
	off: Signal.dotOff,
	green: Signal.green,
	yellow: Signal.yellow,
	red: Signal.red,
};

const STATUS_TEXT_COLORS: Record<SignalColor, string> = {
	red: Signal.red,
	yellow: Signal.yellow,
	green: Signal.green,
	neutral: Signal.text,
};

/** Single-fix GPS timeout — matches the { timeout: 20000 } option in lane-status.html. */
const GPS_TIMEOUT_MS = 20000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const timer = setTimeout(() => reject(new Error("timeout")), ms);
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

export default function LaneStatusScreen() {
	const insets = useSafeAreaInsets();
	const mounted = useRef(true);

	const [screen, setScreen] = useState<Screen>("loading");
	const [errorMsg, setErrorMsg] = useState<string | null>(null);
	const [currentSeq, setCurrentSeq] = useState<CurrentSeq | null>(null);
	const [coords, setCoords] = useState<{ lat: number; lon: number } | null>(null);
	const [laneState, setLaneState] = useState<LaneState | null>(null);

	useEffect(() => {
		mounted.current = true;
		return () => {
			mounted.current = false;
		};
	}, []);

	/**
	 * applySingleFix(lat, lon): pick the lane from one GPS reading. The poller then
	 * drives the lights — a direct port of applySingleFix() in lane-status.html.
	 */
	const applySingleFix = useCallback((lat: number, lon: number, fences: Geofence[]) => {
		setCoords({ lat, lon });
		const lane = findGeofence(lat, lon, fences);
		setCurrentSeq(lane && lane.sequence ? { sequence: lane.sequence, name: lane.name } : null);
		setScreen("ready");
	}, []);

	/**
	 * Load the lane map, then take ONE GPS fix and resolve the lane. Mirrors the
	 * fetch('/api/geofences') → getCurrentPosition → applySingleFix chain on the
	 * web page, including its split error handling:
	 *   - geofence load failures show the raw message,
	 *   - location failures are prefixed with "Location needed: " (onGeoError).
	 */
	const loadAndLocate = useCallback(async () => {
		setScreen("loading");
		setErrorMsg(null);

		let fences: Geofence[];
		try {
			const res = await fetch(`${API_BASE_URL}/api/geofences`);
			if (!res.ok) throw new Error("Could not load lane map");
			const data = await res.json();
			fences = data.geofences ?? [];
		} catch (e) {
			if (!mounted.current) return;
			setScreen("error");
			setErrorMsg(e instanceof Error ? e.message : "Failed to load.");
			return;
		}
		if (!mounted.current) return;

		setScreen("locating");
		try {
			const { status } = await Location.requestForegroundPermissionsAsync();
			if (status !== "granted") {
				throw new Error("allow access when prompted, or enable GPS.");
			}
			// enableHighAccuracy + fresh fix (maximumAge: 0) + 20s timeout, like the web page.
			const pos = await withTimeout(Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High }), GPS_TIMEOUT_MS);
			if (!mounted.current) return;
			applySingleFix(pos.coords.latitude, pos.coords.longitude, fences);
		} catch (e) {
			if (!mounted.current) return;
			let message: string;
			if (e instanceof Error && e.message === "timeout") {
				message = "timed out. Try again with a clear view of the sky.";
			} else if (e instanceof Error && e.message) {
				message = e.message;
			} else {
				message = "allow access when prompted, or enable GPS.";
			}
			setScreen("error");
			setErrorMsg("Location needed: " + message);
		}
	}, [applySingleFix]);

	useEffect(() => {
		loadAndLocate();
	}, [loadAndLocate]);

	/**
	 * startPolling()/stopPolling(): once the sequence is known, fetch /api/state
	 * immediately and then every second, feeding each snapshot to updateLights —
	 * exactly the polling loop in lane-status.html. No sequence ⇒ stopPolling().
	 */
	useEffect(() => {
		const seq = currentSeq?.sequence;
		if (!seq || screen !== "ready") return;

		let active = true;
		const poll = async () => {
			try {
				const res = await fetch(`${API_BASE_URL}/api/state`);
				const state: LaneState = await res.json();
				if (active && mounted.current && !state.error) setLaneState(state);
			} catch {
				/* transient network error: keep last known state, like the web page */
			}
		};

		poll();
		const id = setInterval(poll, STATE_POLL_INTERVAL_MS);
		return () => {
			active = false;
			clearInterval(id);
		};
	}, [currentSeq?.sequence, screen]);

	const seqKey = currentSeq?.sequence ?? null;
	const signal = deriveLaneSignal(laneState, seqKey);

	return (
		<ScrollView style={styles.flex} contentContainerStyle={[styles.scroll, { paddingTop: insets.top + 16, paddingBottom: insets.bottom + 120 }]}>
			<View style={styles.header}>
				<Text style={styles.title}>Your lane status</Text>
				<Text style={styles.subtitle}>Live signal for your approach lane</Text>
			</View>

			<View style={styles.card}>
				{screen === "loading" && <Waiting label="Loading lane map…" />}
				{screen === "locating" && <Waiting label="Getting your location once… If asked, allow access." />}
				{screen === "error" && <Text style={styles.errorText}>{errorMsg ?? "Something went wrong."}</Text>}

				{screen === "ready" && (
					<>
						<View style={styles.seqBox}>
							<Text style={styles.seqHeading}>Your sequence</Text>
							<Text style={styles.seqLabel}>{seqKey ? formatSeqTitle(seqKey, currentSeq?.name) : "Not in a monitored lane"}</Text>
							<View style={[styles.phaseDot, { backgroundColor: DOT_COLORS[signal.phaseDot] }]} />
							<Text style={styles.sessionNote}>Sequence was set from one GPS reading when this screen loaded.</Text>
						</View>

						<View style={styles.lights}>
							<Light color={Signal.red} on={signal.lamp === "red"} />
							<Light color={Signal.yellow} on={signal.lamp === "yellow"} />
							<Light color={Signal.green} on={signal.lamp === "green"} />
						</View>

						<Text style={[styles.statusText, { color: STATUS_TEXT_COLORS[signal.statusColor] }]}>{signal.statusText}</Text>
						{!!signal.timeRemaining && <Text style={styles.timeRemaining}>{signal.timeRemaining}</Text>}
						{!!coords && (
							<Text style={styles.coords}>
								Lat {coords.lat.toFixed(5)}, Lon {coords.lon.toFixed(5)}
							</Text>
						)}
					</>
				)}
			</View>

			<Pressable onPress={loadAndLocate} disabled={screen === "loading" || screen === "locating"} style={({ pressed }) => [styles.refresh, pressed && styles.refreshPressed]}>
				<Text style={styles.refreshText}>{screen === "error" ? "Try again" : "Reload"}</Text>
			</Pressable>
		</ScrollView>
	);
}

function Waiting({ label }: { label: string }) {
	return (
		<View style={styles.waiting}>
			<ActivityIndicator color={Signal.accent} />
			<Text style={styles.waitingText}>{label}</Text>
		</View>
	);
}

function Light({ color, on }: { color: string; on: boolean }) {
	return (
		<View
			style={[
				styles.light,
				on
					? {
							backgroundColor: color,
							borderColor: color,
							shadowColor: color,
							shadowOpacity: 0.9,
							shadowRadius: 16,
							shadowOffset: { width: 0, height: 0 },
							elevation: 10,
						}
					: { backgroundColor: Signal.lampOff },
			]}
		/>
	);
}

const styles = StyleSheet.create({
	flex: { flex: 1, backgroundColor: Palette.canvas },
	scroll: {
		flexGrow: 1,
		alignItems: "center",
		justifyContent: "center",
		paddingHorizontal: 20,
		gap: 18,
	},
	header: { alignItems: "center", gap: 4 },
	title: {
		fontFamily: FontFamily.semiBold,
		fontSize: 24,
		color: Palette.fgStrong,
	},
	subtitle: {
		fontFamily: FontFamily.regular,
		fontSize: 14,
		color: Palette.fgMuted,
	},
	card: {
		width: "100%",
		maxWidth: 380,
		backgroundColor: Signal.card,
		borderRadius: 16,
		borderWidth: 1,
		borderColor: Signal.border,
		padding: 24,
		alignItems: "center",
	},
	seqBox: {
		width: "100%",
		backgroundColor: Signal.cardInner,
		borderRadius: 12,
		borderWidth: 1,
		borderColor: Signal.border,
		paddingVertical: 16,
		paddingHorizontal: 18,
		alignItems: "center",
		marginBottom: 16,
	},
	seqHeading: {
		fontFamily: FontFamily.semiBold,
		fontSize: 12,
		letterSpacing: 0.6,
		textTransform: "uppercase",
		color: Signal.textMuted,
		marginBottom: 8,
	},
	seqLabel: {
		fontFamily: FontFamily.bold,
		fontSize: 18,
		color: Signal.text,
		marginBottom: 12,
		textAlign: "center",
	},
	phaseDot: {
		width: 48,
		height: 48,
		borderRadius: 24,
	},
	sessionNote: {
		fontFamily: FontFamily.regular,
		fontSize: 12,
		lineHeight: 17,
		color: Signal.textMuted,
		textAlign: "center",
		marginTop: 10,
	},
	lights: {
		alignItems: "center",
		gap: 10,
		marginVertical: 14,
	},
	light: {
		width: 56,
		height: 56,
		borderRadius: 28,
		borderWidth: 3,
		borderColor: Signal.border,
	},
	statusText: {
		fontFamily: FontFamily.bold,
		fontSize: 20,
		textAlign: "center",
		marginTop: 8,
	},
	timeRemaining: {
		fontFamily: FontFamily.regular,
		fontSize: 15,
		color: Signal.textSoft,
		textAlign: "center",
		marginTop: 6,
	},
	coords: {
		fontFamily: FontFamily.regular,
		fontSize: 12,
		color: Signal.textMuted,
		marginTop: 10,
	},
	waiting: { alignItems: "center", gap: 12, paddingVertical: 24 },
	waitingText: {
		fontFamily: FontFamily.regular,
		fontSize: 14,
		color: Signal.textSoft,
		textAlign: "center",
	},
	errorText: {
		fontFamily: FontFamily.medium,
		fontSize: 14,
		color: Signal.red,
		textAlign: "center",
		paddingVertical: 16,
	},
	refresh: {
		paddingVertical: 12,
		paddingHorizontal: 20,
		borderRadius: 12,
		backgroundColor: Palette.surface,
	},
	refreshPressed: { opacity: 0.7 },
	refreshText: {
		fontFamily: FontFamily.medium,
		fontSize: 14,
		color: Palette.primary,
	},
});
