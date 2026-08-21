package com.iappyx.scanp208

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.iappyx.scanp208.ui.ScanP208Theme

class MainActivity : ComponentActivity() {

    private var onUsbChange: (() -> Unit)? = null

    private val usbReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                UsbManager.ACTION_USB_DEVICE_ATTACHED,
                UsbManager.ACTION_USB_DEVICE_DETACHED -> onUsbChange?.invoke()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val filter = IntentFilter().apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(usbReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(usbReceiver, filter)
        }

        setContent {
            ScanP208Theme {
                val vm: ScanViewModel = viewModel()
                onUsbChange = { vm.refreshLink() }
                ScanScreen(vm)
            }
        }
    }

    override fun onResume() { super.onResume(); onUsbChange?.invoke() }

    override fun onDestroy() {
        super.onDestroy()
        runCatching { unregisterReceiver(usbReceiver) }
    }
}

// ---------------------------------------------------------------- screen ---

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ScanScreen(vm: ScanViewModel) {
    val s by vm.state.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    val haptics = LocalHapticFeedback.current
    val appBar = TopAppBarDefaults.exitUntilCollapsedScrollBehavior(rememberTopAppBarState())

    LaunchedEffect(s.error) {
        s.error?.let { snackbar.showSnackbar(it); vm.dismissError() }
    }

    Scaffold(
        modifier = Modifier.nestedScroll(appBar.nestedScrollConnection),
        topBar = {
            LargeTopAppBar(
                title = {
                    Column {
                        Text("imageFORMULA", style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text("P-208", style = MaterialTheme.typography.headlineMedium)
                    }
                },
                scrollBehavior = appBar,
                colors = TopAppBarDefaults.largeTopAppBarColors(
                    containerColor = Color.Transparent,
                    scrolledContainerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = .5f)
                )
            )
        },
        bottomBar = { ActionBar(s, vm, haptics) },
        snackbarHost = { SnackbarHost(snackbar) }
    ) { pad ->
        // A tablet is not a tall phone. Past roughly the width of a large
        // phone in landscape there is room for two columns, and stacking
        // everything into one leaves a stretched strip of controls with the
        // switches marooned at the far edge.
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val wide = maxWidth >= 720.dp
            val pages = if (wide) 3 else 2          // thumbnails per row
            val outer = PaddingValues(
                start = 20.dp, end = 20.dp,
                top = pad.calculateTopPadding(),
                bottom = pad.calculateBottomPadding() + 24.dp
            )

            if (!wide) {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = outer,
                    verticalArrangement = Arrangement.spacedBy(20.dp)
                ) {
                    item { LinkCard(s) }
                    if (s.error != null) item { ErrorCard(s, vm) }
                    if (s.log.isNotEmpty()) item { LogCard(s, vm) }
                    item { SettingsCard(s, vm) }
                    if (s.pages.isNotEmpty()) {
                        item { PagesHeader(s, vm) }
                        items(s.pages.chunked(pages)) { row ->
                            PageRow(row, pages, s, vm, haptics)
                        }
                        item { SaveCard(s, vm) }
                    }
                }
            } else {
                Row(
                    modifier = Modifier.fillMaxSize().padding(outer),
                    horizontalArrangement = Arrangement.spacedBy(24.dp)
                ) {
                    // settings on the left, at a readable width rather than
                    // stretched across the whole panel
                    LazyColumn(
                        modifier = Modifier.width(420.dp).fillMaxHeight(),
                        verticalArrangement = Arrangement.spacedBy(20.dp)
                    ) {
                        item { LinkCard(s) }
                        if (s.error != null) item { ErrorCard(s, vm) }
                        if (s.log.isNotEmpty()) item { LogCard(s, vm) }
                        item { SettingsCard(s, vm) }
                    }
                    // the scanned pages get the rest
                    LazyColumn(
                        modifier = Modifier.weight(1f).fillMaxHeight(),
                        verticalArrangement = Arrangement.spacedBy(16.dp)
                    ) {
                        if (s.pages.isEmpty()) {
                            item { EmptyPages() }
                        } else {
                            item { PagesHeader(s, vm) }
                            items(s.pages.chunked(pages)) { row ->
                                PageRow(row, pages, s, vm, haptics)
                            }
                            item { SaveCard(s, vm) }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PageRow(
    row: List<Page>, per: Int, s: UiState, vm: ScanViewModel,
    haptics: androidx.compose.ui.hapticfeedback.HapticFeedback
) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        row.forEach { p ->
            Box(Modifier.weight(1f)) { PageCard(p, s.selected == p.id, vm, haptics) }
        }
        repeat(per - row.size) { Spacer(Modifier.weight(1f)) }
    }
}

/** Placeholder so the page column is not a blank void before the first scan. */
@Composable
private fun EmptyPages() {
    Column(
        modifier = Modifier.fillMaxWidth().padding(top = 80.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Icon(
            Icons.Rounded.DocumentScanner, null,
            Modifier.size(44.dp),
            tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = .5f)
        )
        Text("No pages yet", style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text("Load the feeder and tap Scan.", style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = .7f))
    }
}

// ------------------------------------------------------------ connection ---

@Composable
private fun LinkCard(s: UiState) {
    val (icon, title, body, tone) = when (s.link) {
        Link.READY -> Quad(
            Icons.Rounded.CheckCircle, "Scanner ready",
            s.model.ifEmpty { "Connected over USB" }, MaterialTheme.colorScheme.primaryContainer
        )
        Link.SCANNING -> Quad(
            Icons.Rounded.Autorenew, s.status.ifEmpty { "Scanning…" },
            if (s.bytes > 0) "%.1f MB read".format(s.bytes / 1_048_576.0) else "Hold on",
            MaterialTheme.colorScheme.tertiaryContainer
        )
        Link.STORAGE_MODE -> Quad(
            Icons.Rounded.Usb, "Auto Start is ON",
            "The scanner is acting as a USB stick. Switch Auto Start OFF on the back, then re-plug the cable.",
            MaterialTheme.colorScheme.errorContainer
        )
        Link.NEEDS_PERMISSION -> Quad(
            Icons.Rounded.Lock, "Permission needed",
            "Allow this app to use the scanner when Android asks.",
            MaterialTheme.colorScheme.secondaryContainer
        )
        Link.SEARCHING -> Quad(
            Icons.Rounded.SearchOff, "No scanner",
            "Plug the P-208 in with a USB-C cable.",
            MaterialTheme.colorScheme.surfaceVariant
        )
    }

    Surface(
        color = tone,
        shape = RoundedCornerShape(28.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            Modifier.padding(20.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            if (s.link == Link.SCANNING) {
                val spin by rememberInfiniteTransition("spin").animateFloat(
                    0f, 360f, infiniteRepeatable(tween(1400, easing = LinearEasing)), label = "a"
                )
                Icon(icon, null, Modifier.size(28.dp).rotate(spin))
            } else {
                Icon(icon, null, Modifier.size(28.dp))
            }
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Text(body, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

private data class Quad<A, B, C, D>(val a: A, val b: B, val c: C, val d: D)

@Composable
private fun ErrorCard(s: UiState, vm: ScanViewModel) {
    Surface(
        color = MaterialTheme.colorScheme.errorContainer,
        shape = RoundedCornerShape(24.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(Modifier.padding(20.dp), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Icon(Icons.Rounded.ErrorOutline, null, Modifier.size(24.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("That did not work", style = MaterialTheme.typography.titleMedium)
                Text(s.error ?: "", style = MaterialTheme.typography.bodyMedium)
            }
            IconButton(onClick = { vm.dismissError() }) { Icon(Icons.Rounded.Close, "Dismiss") }
        }
    }
}

/**
 * The scan happens with the USB port occupied by the scanner, so there is no
 * way to watch it from a computer. The log is shown here instead, and can be
 * copied out.
 */
@Composable
private fun LogCard(s: UiState, vm: ScanViewModel) {
    val clip = androidx.compose.ui.platform.LocalClipboardManager.current
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = .5f),
        shape = RoundedCornerShape(24.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(vertical = 8.dp)) {
            Row(
                Modifier.fillMaxWidth().clickable { vm.toggleLog() }
                    .padding(horizontal = 20.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(Icons.Rounded.Terminal, null, Modifier.size(20.dp))
                Spacer(Modifier.width(12.dp))
                Text("Details", Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
                Text("${s.log.size} lines", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.width(8.dp))
                Icon(
                    if (s.showLog) Icons.Rounded.ExpandLess else Icons.Rounded.ExpandMore,
                    null, Modifier.size(20.dp)
                )
            }
            AnimatedVisibility(s.showLog) {
                Column {
                    Column(
                        Modifier.fillMaxWidth()
                            .heightIn(max = 320.dp)
                            .verticalScroll(rememberScrollState())
                            .padding(horizontal = 20.dp, vertical = 4.dp)
                    ) {
                        s.log.forEach {
                            Text(
                                it,
                                style = MaterialTheme.typography.bodySmall.copy(
                                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                                ),
                                color = if (it.contains("!!")) MaterialTheme.colorScheme.error
                                        else MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                    TextButton(
                        onClick = { clip.setText(androidx.compose.ui.text.AnnotatedString(vm.logText())) },
                        modifier = Modifier.padding(start = 12.dp)
                    ) {
                        Icon(Icons.Rounded.ContentCopy, null, Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Copy log")
                    }
                }
            }
        }
    }
}

// -------------------------------------------------------------- settings ---

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun SettingsCard(s: UiState, vm: ScanViewModel) {
    val enabled = s.link != Link.SCANNING

    Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
        SectionLabel("Resolution")
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
            ScannerDriver.DPIS.forEachIndexed { i, dpi ->
                SegmentedButton(
                    selected = s.settings.dpi == dpi,
                    onClick = { vm.update { st -> st.copy(settings = st.settings.copy(dpi = dpi)) } },
                    enabled = enabled,
                    shape = SegmentedButtonDefaults.itemShape(i, ScannerDriver.DPIS.size),
                    label = { Text("$dpi", maxLines = 1) }
                )
            }
        }
        Text(
            when (s.settings.dpi) {
                300, 600 -> "Native — scanned exactly at this resolution."
                else -> "Scanned at ${ScannerDriver.SCAN_PLAN[s.settings.dpi]?.first ?: s.settings.dpi} dpi and resampled, which this scanner needs to stay square."
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        SectionLabel("Colour")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ScanMode.entries.forEach { m ->
                FilterChip(
                    selected = s.settings.mode == m,
                    onClick = { vm.update { st -> st.copy(settings = st.settings.copy(mode = m)) } },
                    enabled = enabled,
                    label = { Text(m.label) },
                    leadingIcon = if (s.settings.mode == m) {
                        { Icon(Icons.Rounded.Check, null, Modifier.size(18.dp)) }
                    } else null
                )
            }
        }

        AnimatedVisibility(s.settings.mode == ScanMode.GRAY) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SectionLabel("Drop a colour out")
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Dropout.entries.forEach { d ->
                        FilterChip(
                            selected = s.settings.dropout == d,
                            onClick = { vm.update { st -> st.copy(settings = st.settings.copy(dropout = d)) } },
                            enabled = enabled,
                            label = { Text(d.label) }
                        )
                    }
                }
            }
        }

        SectionLabel("Options")
        Surface(
            shape = RoundedCornerShape(24.dp),
            color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = .45f),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(Modifier.padding(vertical = 4.dp)) {
                Toggle("Scan both sides", "Front and back in one pass",
                    s.settings.duplex, enabled) { v ->
                    vm.update { st -> st.copy(settings = st.settings.copy(duplex = v)) }
                }
                Toggle("Detect page size", "Let the scanner measure the sheet",
                    s.settings.autosize, enabled) { v ->
                    vm.update { st -> st.copy(settings = st.settings.copy(autosize = v)) }
                }
                Toggle("Trim to the sheet", "Crop the backing away after scanning",
                    s.autocrop, enabled) { v -> vm.update { st -> st.copy(autocrop = v) } }
                Toggle("Skip blank sides", "Drop pages with nothing on them",
                    s.skipBlank, enabled) { v -> vm.update { st -> st.copy(skipBlank = v) } }
                Toggle("Stack as one long image", "For a receipt, or a document that should stay in one piece",
                    s.settings.continuous, enabled) { v ->
                    vm.update { st -> st.copy(settings = st.settings.copy(continuous = v)) }
                }
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) = Text(
    text.uppercase(),
    style = MaterialTheme.typography.labelSmall,
    color = MaterialTheme.colorScheme.onSurfaceVariant,
    modifier = Modifier.padding(start = 4.dp)
)

@Composable
private fun Toggle(
    title: String, subtitle: String, checked: Boolean, enabled: Boolean,
    onChange: (Boolean) -> Unit
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable(enabled = enabled) { onChange(!checked) }
            .padding(horizontal = 20.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = onChange, enabled = enabled)
    }
}

// ----------------------------------------------------------------- pages ---

@Composable
private fun PagesHeader(s: UiState, vm: ScanViewModel) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text("${s.pages.size} page${if (s.pages.size == 1) "" else "s"}",
            style = MaterialTheme.typography.titleLarge)
        TextButton(onClick = { vm.clearAll() }, enabled = s.link != Link.SCANNING) {
            Icon(Icons.Rounded.DeleteSweep, null, Modifier.size(18.dp))
            Spacer(Modifier.width(6.dp))
            Text("Clear")
        }
    }
}

@Composable
private fun PageCard(
    p: Page, selected: Boolean, vm: ScanViewModel,
    haptics: androidx.compose.ui.hapticfeedback.HapticFeedback
) {
    val border by animateDpAsState(if (selected) 3.dp else 0.dp, label = "b")
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(
            shape = RoundedCornerShape(18.dp),
            tonalElevation = 3.dp,
            shadowElevation = if (selected) 6.dp else 1.dp,
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(0.74f)
                .border(border, MaterialTheme.colorScheme.primary, RoundedCornerShape(18.dp))
                .clickable {
                    haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                    vm.select(p.id)
                }
        ) {
            androidx.compose.foundation.Image(
                bitmap = p.thumb.asImageBitmap(),
                contentDescription = "Page ${p.sheet + 1}",
                contentScale = ContentScale.Fit,
                modifier = Modifier.fillMaxSize().padding(6.dp)
            )
        }
        AnimatedVisibility(selected) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.padding(top = 6.dp)
            ) {
                FilledTonalIconButton(onClick = { vm.rotate(p.id, -90) }, Modifier.size(38.dp)) {
                    Icon(Icons.Rounded.RotateLeft, "Rotate left", Modifier.size(18.dp))
                }
                FilledTonalIconButton(onClick = { vm.rotate(p.id, 90) }, Modifier.size(38.dp)) {
                    Icon(Icons.Rounded.RotateRight, "Rotate right", Modifier.size(18.dp))
                }
                FilledTonalIconButton(
                    onClick = { vm.delete(p.id) },
                    modifier = Modifier.size(38.dp),
                    colors = IconButtonDefaults.filledTonalIconButtonColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer,
                        contentColor = MaterialTheme.colorScheme.onErrorContainer
                    )
                ) { Icon(Icons.Rounded.Delete, "Delete", Modifier.size(18.dp)) }
            }
        }
    }
}

// ------------------------------------------------------------------ save ---

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SaveCard(s: UiState, vm: ScanViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        SectionLabel("Save as")
        OutlinedTextField(
            value = s.name,
            onValueChange = { v -> vm.update { it.copy(name = v.ifBlank { "Scan" }) } },
            label = { Text("File name") },
            singleLine = true,
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier.fillMaxWidth()
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SaveFormat.entries.forEach { f ->
                FilterChip(
                    selected = s.format == f,
                    onClick = { vm.update { it.copy(format = f) } },
                    label = { Text(f.label) }
                )
            }
        }
        AnimatedVisibility(s.format == SaveFormat.PDF) {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                SectionLabel("PDF quality")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    PdfWriter.QUALITY.keys.forEach { q ->
                        FilterChip(
                            selected = s.pdfQuality == q,
                            onClick = { vm.update { it.copy(pdfQuality = q) } },
                            label = { Text(q.replaceFirstChar { c -> c.uppercase() }) }
                        )
                    }
                }
                Text(
                    when (s.pdfQuality) {
                        "max" -> "No loss at all. Much larger files."
                        "high" -> "Differences are not visible on paper."
                        "small" -> "Email-sized. Text stays crisp, photos soften."
                        else -> "What document scanners normally ship with."
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(
                onClick = { vm.save(false) },
                enabled = !s.saving,
                modifier = Modifier.weight(1f).height(52.dp),
                shape = RoundedCornerShape(16.dp)
            ) {
                if (s.saving) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp,
                        color = MaterialTheme.colorScheme.onPrimary)
                } else {
                    Icon(Icons.Rounded.Save, null, Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("Save")
                }
            }
            OutlinedButton(
                onClick = { vm.save(true) },
                enabled = !s.saving,
                modifier = Modifier.weight(1f).height(52.dp),
                shape = RoundedCornerShape(16.dp)
            ) {
                Icon(Icons.Rounded.Share, null, Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text("Share")
            }
        }
    }
}

// ------------------------------------------------------------ action bar ---

@Composable
private fun ActionBar(
    s: UiState, vm: ScanViewModel,
    haptics: androidx.compose.ui.hapticfeedback.HapticFeedback
) {
    Surface(
        tonalElevation = 3.dp,
        color = MaterialTheme.colorScheme.surface
    ) {
        Box(Modifier.navigationBarsPadding().padding(20.dp)) {
            if (s.link == Link.SCANNING) {
                OutlinedButton(
                    onClick = { vm.cancel() },
                    modifier = Modifier.fillMaxWidth().height(60.dp),
                    shape = RoundedCornerShape(20.dp)
                ) {
                    Icon(Icons.Rounded.Stop, null); Spacer(Modifier.width(10.dp))
                    Text("Stop", style = MaterialTheme.typography.titleMedium)
                }
            } else {
                Button(
                    onClick = {
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                        vm.startScan()
                    },
                    enabled = s.link == Link.READY,
                    modifier = Modifier.fillMaxWidth().height(60.dp),
                    shape = RoundedCornerShape(20.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary
                    )
                ) {
                    Icon(Icons.Rounded.DocumentScanner, null)
                    Spacer(Modifier.width(10.dp))
                    Text(
                        if (s.pages.isEmpty()) "Scan" else "Scan more",
                        style = MaterialTheme.typography.titleMedium
                    )
                }
            }
        }
    }
}
