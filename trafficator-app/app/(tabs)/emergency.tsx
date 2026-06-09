import { MaterialCommunityIcons } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import { useState } from 'react';
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { API_BASE_URL } from '@/constants/config';
import { FontFamily, Palette } from '@/constants/theme';
import { getCurrentFix } from '@/lib/location';

type Service = {
  label: string;
  icon: keyof typeof MaterialCommunityIcons.glyphMap;
  color: string;
};

const SERVICES: Service[] = [
  { label: 'Police', icon: 'police-badge', color: Palette.primary },
  { label: 'Ambulance', icon: 'ambulance', color: Palette.danger },
];

export default function EmergencyScreen() {
  const insets = useSafeAreaInsets();
  const [pending, setPending] = useState<string | null>(null);

  /**
   * Take one GPS fix and POST it with the button text to /api/emergency. The
   * coordinator resolves which lane the phone is in and triggers that sequence.
   */
  const dispatch = async (service: Service) => {
    setPending(service.label);
    try {
      const fix = await getCurrentFix();
      const res = await fetch(`${API_BASE_URL}/api/emergency`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: fix.lat, lon: fix.lon, source: service.label }),
      });
      const data = await res.json().catch(() => ({}));

      if (res.ok && data.ok) {
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
        Alert.alert(
          `${service.label} requested`,
          `Prioritizing ${data.name || data.sequence}. Help is on the way — stay safe.`,
        );
      } else if (res.status === 404 || data.error === 'not_in_lane') {
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
        Alert.alert(
          'Not in a monitored lane',
          data.message || 'Move into an approach lane and try again.',
        );
      } else {
        throw new Error(data.error || `Request failed (${res.status})`);
      }
    } catch (e) {
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
      Alert.alert('Could not send request', e instanceof Error ? e.message : 'Please try again.');
    } finally {
      setPending(null);
    }
  };

  const confirm = (service: Service) => {
    if (pending) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    Alert.alert(
      `Request ${service.label}?`,
      `We'll use your location to prioritize your lane and notify ${service.label.toLowerCase()} dispatch.`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: `Request ${service.label}`,
          style: 'destructive',
          onPress: () => dispatch(service),
        },
      ],
    );
  };

  return (
    <View
      style={[
        styles.container,
        { paddingTop: insets.top + 16, paddingBottom: insets.bottom + 120 },
      ]}>
      <View style={styles.header}>
        <Text style={styles.title}>Emergency Services</Text>
        <Text style={styles.subtitle}>Tap to prioritize your lane</Text>
      </View>

      <View style={styles.buttons}>
        {SERVICES.map((service) => {
          const isPending = pending === service.label;
          const disabled = pending !== null;
          return (
            <Pressable
              key={service.label}
              onPress={() => confirm(service)}
              disabled={disabled}
              style={({ pressed }) => [
                styles.button,
                { backgroundColor: service.color },
                pressed && styles.buttonPressed,
                disabled && !isPending && styles.buttonDimmed,
              ]}>
              {isPending ? (
                <ActivityIndicator color="#fff" size="large" />
              ) : (
                <MaterialCommunityIcons name={service.icon} size={40} color="#fff" />
              )}
              <Text style={styles.buttonText}>
                {isPending ? 'Sending…' : service.label}
              </Text>
            </Pressable>
          );
        })}
      </View>

      <Text style={styles.note}>Your location is shared only when you make a request.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: Palette.canvas,
    paddingHorizontal: 20,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 28,
  },
  header: { alignItems: 'center', gap: 4 },
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
  buttons: {
    width: '100%',
    maxWidth: 380,
    gap: 20,
  },
  button: {
    height: 130,
    borderRadius: 24,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
    shadowColor: '#000',
    shadowOpacity: 0.18,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 8 },
    elevation: 6,
  },
  buttonPressed: {
    opacity: 0.85,
    transform: [{ scale: 0.98 }],
  },
  buttonDimmed: {
    opacity: 0.5,
  },
  buttonText: {
    fontFamily: FontFamily.semiBold,
    fontSize: 22,
    color: '#fff',
    letterSpacing: 0.3,
  },
  note: {
    fontFamily: FontFamily.regular,
    fontSize: 12,
    color: Palette.fgSubtle,
    textAlign: 'center',
  },
});
