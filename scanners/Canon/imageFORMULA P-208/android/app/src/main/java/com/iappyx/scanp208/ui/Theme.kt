package com.iappyx.scanp208.ui

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat

/**
 * The palette used when the platform has no dynamic colour to offer.
 *
 * Ink on paper: a deep blue-slate against warm off-white, with amber reserved
 * for the one action that moves the motor.
 */
private val Ink = Color(0xFF2B4A6F)
private val InkLight = Color(0xFFD3E3FB)
private val Amber = Color(0xFF9A5B00)
private val AmberLight = Color(0xFFFFDCBE)
private val Paper = Color(0xFFFBF8F4)
private val PaperDim = Color(0xFFF1EDE7)

private val LightScheme = lightColorScheme(
    primary = Ink,
    onPrimary = Color.White,
    primaryContainer = InkLight,
    onPrimaryContainer = Color(0xFF0B1B2E),
    secondary = Color(0xFF52606F),
    secondaryContainer = Color(0xFFD6E4F3),
    onSecondaryContainer = Color(0xFF101C27),
    tertiary = Amber,
    onTertiary = Color.White,
    tertiaryContainer = AmberLight,
    onTertiaryContainer = Color(0xFF321300),
    background = Paper,
    onBackground = Color(0xFF1A1C1E),
    surface = Paper,
    onSurface = Color(0xFF1A1C1E),
    surfaceVariant = PaperDim,
    onSurfaceVariant = Color(0xFF43474E),
    outline = Color(0xFF73777F),
    outlineVariant = Color(0xFFC3C7CF)
)

private val DarkScheme = darkColorScheme(
    primary = Color(0xFFA3C8F0),
    onPrimary = Color(0xFF0B2745),
    primaryContainer = Color(0xFF123A5F),
    onPrimaryContainer = InkLight,
    secondary = Color(0xFFB9C8D7),
    secondaryContainer = Color(0xFF394856),
    onSecondaryContainer = Color(0xFFD6E4F3),
    tertiary = Color(0xFFFFB870),
    onTertiary = Color(0xFF522300),
    tertiaryContainer = Color(0xFF753600),
    onTertiaryContainer = AmberLight,
    background = Color(0xFF121417),
    onBackground = Color(0xFFE3E2E6),
    surface = Color(0xFF121417),
    onSurface = Color(0xFFE3E2E6),
    surfaceVariant = Color(0xFF43474E),
    onSurfaceVariant = Color(0xFFC3C7CF),
    outline = Color(0xFF8D9199),
    outlineVariant = Color(0xFF43474E)
)

private val AppTypography = Typography().let { t ->
    t.copy(
        displaySmall = t.displaySmall.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.5).sp),
        headlineMedium = t.headlineMedium.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.4).sp),
        headlineSmall = t.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
        titleLarge = t.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        titleMedium = t.titleMedium.copy(fontWeight = FontWeight.Medium),
        labelLarge = t.labelLarge.copy(fontWeight = FontWeight.SemiBold, letterSpacing = 0.2.sp),
        labelSmall = t.labelSmall.copy(letterSpacing = 0.6.sp)
    )
}

/** Tabular figures for anything that counts up, so the layout does not jitter. */
val MonoNumerals = TextStyle(fontFamily = FontFamily.Monospace)

@Composable
fun ScanP208Theme(
    dark: Boolean = isSystemInDarkTheme(),
    dynamic: Boolean = true,
    content: @Composable () -> Unit
) {
    val ctx = LocalContext.current
    val scheme = when {
        dynamic && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
        dark -> DarkScheme
        else -> LightScheme
    }

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            window.statusBarColor = Color.Transparent.toArgb()
            window.navigationBarColor = Color.Transparent.toArgb()
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !dark
                isAppearanceLightNavigationBars = !dark
            }
        }
    }

    MaterialTheme(colorScheme = scheme, typography = AppTypography, content = content)
}
