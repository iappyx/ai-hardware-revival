package com.iappyx.scanp208

import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

enum class Link { SEARCHING, STORAGE_MODE, NEEDS_PERMISSION, READY, SCANNING }

/**
 * One scanned side.
 *
 * The full-resolution page lives in a cache file rather than in memory: a
 * 300 dpi colour A4 is about 36 MB as a Bitmap, so a ten-sheet stack would not
 * fit. Only the thumbnail is held.
 */
data class Page(
    val id: Long,
    val file: File,
    val thumb: Bitmap,
    val rotation: Int = 0,
    val sheet: Int = 0,
    val side: Int = 0
)

data class UiState(
    val link: Link = Link.SEARCHING,
    val model: String = "",
    val settings: ScanSettings = ScanSettings(),
    val pages: List<Page> = emptyList(),
    val selected: Long? = null,
    val status: String = "",
    val bytes: Long = 0,
    val error: String? = null,
    val log: List<String> = emptyList(),
    val showLog: Boolean = false,
    val saving: Boolean = false,
    val savedCount: Int = 0,
    val name: String = "Scan",
    val format: SaveFormat = SaveFormat.PDF,
    val pdfQuality: String = PdfWriter.DEFAULT_QUALITY,
    val autocrop: Boolean = true,
    val skipBlank: Boolean = false
)

class ScanViewModel(app: Application) : AndroidViewModel(app) {

    private val transport = UsbTransport(app)
    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var job: Job? = null
    private var nextId = 1L

    init {
        Diag.init(app)
        refreshLink()
    }

    fun refreshLink() {
        val dev = transport.findDevice()
        _state.update {
            it.copy(
                link = when {
                    _state.value.link == Link.SCANNING -> Link.SCANNING
                    dev == null && transport.isInStorageMode() -> Link.STORAGE_MODE
                    dev == null -> Link.SEARCHING
                    else -> Link.READY
                }
            )
        }
    }

    fun update(f: (UiState) -> UiState) = _state.update(f)

    fun dismissError() = _state.update { it.copy(error = null) }

    fun toggleLog() = _state.update { it.copy(showLog = !it.showLog, log = Diag.snapshot()) }

    fun logText(): String = Diag.text()

    fun startScan() {
        if (job?.isActive == true) return
        val dev = transport.findDevice()
        if (dev == null) {
            _state.update {
                it.copy(
                    error = if (transport.isInStorageMode())
                        "The Auto Start switch on the back is ON, so the scanner is acting as a USB stick. Switch it OFF and re-plug."
                    else "No scanner found. Check the cable."
                )
            }
            return
        }

        job = viewModelScope.launch {
            if (!transport.requestPermission(dev)) {
                _state.update { it.copy(error = "USB permission was declined.", link = Link.READY) }
                return@launch
            }
            Diag.reset()
            Diag.d("scan requested: ${_state.value.settings}")
            _state.update {
                it.copy(link = Link.SCANNING, status = "Connecting…", bytes = 0,
                        error = null, log = emptyList())
            }

            withContext(Dispatchers.IO) {
                try {
                    transport.open()
                    val driver = ScannerDriver(transport)
                    val id = runCatching { driver.inquiry() }.getOrNull()
                    if (id != null) _state.update { it.copy(model = "${id.vendor} ${id.product} rev ${id.revision}") }

                    val settings = _state.value.settings
                    driver.scanBatch(settings, listener = object : ScannerDriver.Listener {
                        override fun onStatus(text: String) {
                            Diag.d("status: $text")
                            _state.update { it.copy(status = text, log = Diag.snapshot()) }
                        }

                        override fun onProgressBytes(bytes: Long) =
                            _state.update { it.copy(bytes = bytes) }

                        override fun onPage(index: Int, side: Int, bitmap: Bitmap) {
                            var bmp = bitmap
                            if (_state.value.autocrop) bmp = Imaging.autocrop(bmp)
                            if (_state.value.skipBlank && Imaging.isBlank(bmp)) {
                                bmp.recycle(); return
                            }
                            val thumb = Imaging.thumbnail(bmp)
                            val file = cacheFile()
                            writeLossless(bmp, file)
                            bmp.recycle()
                            Diag.d("page ${index + 1} side $side -> ${thumb.width}x${thumb.height} thumb")
                            val page = Page(nextId++, file, thumb, 0, index, side)
                            _state.update { it.copy(pages = it.pages + page) }
                        }
                    })
                    Diag.d("batch finished with ${_state.value.pages.size} page(s)")
                    _state.update { it.copy(status = "Done — ${it.pages.size} page(s)") }
                } catch (e: NotFound) {
                    Diag.e("not found", e)
                    _state.update { it.copy(error = e.message) }
                } catch (e: Busy) {
                    Diag.e("busy", e)
                    _state.update { it.copy(error = e.message) }
                } catch (e: Throwable) {
                    Diag.e("scan failed", e)
                    val msg = e.message ?: e.toString()
                    _state.update {
                        it.copy(
                            error = if (msg.contains("no documents", true))
                                "Nothing in the feeder — load some paper and try again."
                            else "${e.javaClass.simpleName}: $msg"
                        )
                    }
                } finally {
                    runCatching { transport.close() }
                    Diag.d("device closed")
                    _state.update { it.copy(link = Link.READY, log = Diag.snapshot()) }
                }
            }
        }
    }

    fun cancel() {
        job?.cancel()
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { ScannerDriver(transport).stopBatch() }
            runCatching { transport.close() }
            _state.update { it.copy(link = Link.READY, status = "Stopped") }
        }
    }

    // ---- page editing ----------------------------------------------------

    fun select(id: Long?) = _state.update { it.copy(selected = if (it.selected == id) null else id) }

    fun rotate(id: Long, by: Int = 90) = _state.update { s ->
        s.copy(pages = s.pages.map {
            if (it.id == id) it.copy(
                rotation = (it.rotation + by) % 360,
                thumb = Imaging.rotate(it.thumb.copy(Bitmap.Config.ARGB_8888, false), by)
            ) else it
        })
    }

    fun delete(id: Long) = _state.update { s ->
        s.pages.firstOrNull { it.id == id }?.let { runCatching { it.file.delete() } }
        s.copy(pages = s.pages.filterNot { it.id == id }, selected = null)
    }

    fun clearAll() = _state.update { s ->
        s.pages.forEach { runCatching { it.file.delete() } }
        s.copy(pages = emptyList(), selected = null, status = "", bytes = 0)
    }

    // ---- saving ----------------------------------------------------------

    fun save(share: Boolean = false) {
        val s = _state.value
        if (s.pages.isEmpty() || s.saving) return
        viewModelScope.launch {
            _state.update { it.copy(saving = true) }
            val uris = withContext(Dispatchers.IO) {
                val bitmaps = s.pages.mapNotNull { p ->
                    BitmapFactory.decodeFile(p.file.absolutePath)?.let { b ->
                        if (p.rotation != 0) Imaging.rotate(b, p.rotation) else b
                    }
                }
                try {
                    if (s.format == SaveFormat.PDF) {
                        listOfNotNull(MediaSaver.savePdf(getApplication(), bitmaps, s.name, s.pdfQuality))
                    } else {
                        MediaSaver.saveImages(getApplication(), bitmaps, s.name, s.format)
                    }
                } finally {
                    bitmaps.forEach { it.recycle() }
                }
            }
            _state.update {
                it.copy(
                    saving = false,
                    savedCount = uris.size,
                    status = if (uris.isEmpty()) "Could not save" else "Saved ${uris.size} file(s)",
                    error = if (uris.isEmpty()) "Nothing could be written. Check storage permissions." else null
                )
            }
            if (share && uris.isNotEmpty()) {
                MediaSaver.share(getApplication(), uris, s.format.mime)
            }
        }
    }

    // ---- cache -----------------------------------------------------------

    private fun cacheFile(): File {
        val dir = File(getApplication<Application>().cacheDir, "pages").apply { mkdirs() }
        return File(dir, "page-${System.nanoTime()}.bin")
    }

    /**
     * Cache a page without throwing away quality. WEBP lossless is both smaller
     * and faster than PNG where it exists; below API 30 there is only PNG.
     */
    private fun writeLossless(bmp: Bitmap, file: File) {
        file.outputStream().use { out ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                bmp.compress(Bitmap.CompressFormat.WEBP_LOSSLESS, 100, out)
            } else {
                bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        runCatching { transport.close() }
        _state.value.pages.forEach { runCatching { it.file.delete() } }
    }
}
