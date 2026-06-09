import { BottomTabBarProps } from '@react-navigation/bottom-tabs';
import { PlatformPressable } from '@react-navigation/elements';
import { useLinkBuilder } from '@react-navigation/native';
import { BlurView } from 'expo-blur';
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import { Platform, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { FontFamily, Palette } from '@/constants/theme';

const PILL_HEIGHT = 66;

/**
 * Floating "liquid glass" tab bar inspired by the Bitafrika mobile app:
 * an absolutely-positioned frosted pill that hovers above the content with
 * a soft blue active highlight and haptic feedback on press.
 */
export function GlassTabBar({ state, descriptors, navigation }: BottomTabBarProps) {
  const { buildHref } = useLinkBuilder();
  const insets = useSafeAreaInsets();

  return (
    <View
      pointerEvents="box-none"
      style={[styles.row, { bottom: Math.max(insets.bottom, 16) + 8 }]}>
      <View style={[styles.glassWrapper, { borderRadius: PILL_HEIGHT / 2 }]}>
        <BlurView
          intensity={Platform.OS === 'ios' ? 24 : 0}
          tint={Platform.OS === 'ios' ? 'systemChromeMaterialLight' : 'light'}
          style={[StyleSheet.absoluteFill, { borderRadius: PILL_HEIGHT / 2 }]}
        />
        <View
          pointerEvents="none"
          style={[
            StyleSheet.absoluteFill,
            {
              borderRadius: PILL_HEIGHT / 2,
              backgroundColor:
                Platform.OS === 'android' ? Palette.glassTintAndroid : Palette.glassTintIOS,
            },
          ]}
        />
        <LinearGradient
          pointerEvents="none"
          colors={['rgba(255,255,255,0.55)', 'rgba(255,255,255,0.18)', 'rgba(255,255,255,0)']}
          locations={[0, 0.35, 1]}
          start={{ x: 0.5, y: 0 }}
          end={{ x: 0.5, y: 1 }}
          style={[StyleSheet.absoluteFill, { borderRadius: PILL_HEIGHT / 2 }]}
        />
        <View
          pointerEvents="none"
          style={[
            StyleSheet.absoluteFill,
            { borderRadius: PILL_HEIGHT / 2, borderWidth: 1, borderColor: Palette.glassBorder },
          ]}
        />

        <View style={styles.content}>
          {state.routes.map((route, index) => {
            const { options } = descriptors[route.key];
            const isFocused = state.index === index;
            const color = isFocused ? Palette.primary : Palette.fgFaint;

            const label =
              typeof options.tabBarLabel === 'string'
                ? options.tabBarLabel
                : (options.title ?? route.name);

            const onPress = () => {
              Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
              const event = navigation.emit({
                type: 'tabPress',
                target: route.key,
                canPreventDefault: true,
              });
              if (!isFocused && !event.defaultPrevented) {
                navigation.navigate(route.name, route.params);
              }
            };

            const onLongPress = () => {
              Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Soft);
              navigation.emit({ type: 'tabLongPress', target: route.key });
            };

            return (
              <PlatformPressable
                key={route.key}
                href={buildHref(route.name, route.params)}
                accessibilityRole="button"
                accessibilityState={isFocused ? { selected: true } : {}}
                accessibilityLabel={options.tabBarAccessibilityLabel}
                onPress={onPress}
                onLongPress={onLongPress}
                android_ripple={{ color: 'rgba(21, 94, 239, 0.15)', borderless: true }}
                style={[styles.tabItem, isFocused && styles.tabItemActive]}>
                {options.tabBarIcon?.({ focused: isFocused, color, size: 22 })}
                <Text
                  numberOfLines={1}
                  style={[
                    styles.label,
                    { color, fontFamily: isFocused ? FontFamily.semiBold : FontFamily.medium },
                  ]}>
                  {label}
                </Text>
              </PlatformPressable>
            );
          })}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    position: 'absolute',
    left: 16,
    right: 16,
    zIndex: 1,
  },
  glassWrapper: {
    height: PILL_HEIGHT,
    overflow: 'hidden',
    backgroundColor: Platform.OS === 'android' ? 'rgba(232, 238, 255, 0.20)' : 'transparent',
    shadowColor: '#1a3a8f',
    shadowOpacity: 0.16,
    shadowRadius: 22,
    shadowOffset: { width: 0, height: 8 },
    elevation: 10,
  },
  content: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-around',
    paddingHorizontal: 6,
  },
  tabItem: {
    flex: 1,
    height: PILL_HEIGHT - 12,
    marginHorizontal: 4,
    borderRadius: (PILL_HEIGHT - 12) / 2,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
  },
  tabItemActive: {
    backgroundColor: Palette.primarySoft,
  },
  label: {
    fontSize: 13,
    letterSpacing: 0.1,
  },
});
