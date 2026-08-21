package com.iappyx.scanp208

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.pdf.PdfDocument
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.core.content.FileProvider
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStream

enum class SaveFormat(val label: String, val ext: String, val mime: String) {
    JPEG("JPEG", "jpg", "image/jpeg"),
    PNG("PNG", "png", "image/png"),
    PDF("PDF", "pdf", "application/pdf")
}

/**
 * Writing finished pages out.
 *
 * Images go to the shared Pictures collection and PDFs to Documents, so they
 * appear in the gallery and the Files app without the user hunting for them.
 */
object MediaSaver {

    private const val ALBUM = "P-208 Scans"

    private fun stamp(): String =
        android.text.format.DateFormat.format("yyyyMMdd-HHmmss", java.util.Date()).toString()

    private fun openImageSink(ctx: Context, name: String, fmt: SaveFormat): Pair<Uri, OutputStream>? {
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, fmt.mime)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                put(MediaStore.MediaColumns.RELATIVE_PATH, "${Environment.DIRECTORY_PICTURES}/$ALBUM")
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
        }
        val uri = ctx.contentResolver.insert(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values
        ) ?: return null
        val out = ctx.contentResolver.openOutputStream(uri) ?: return null
        return uri to out
    }

    private fun finish(ctx: Context, uri: Uri) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ctx.contentResolver.update(
                uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null
            )
        }
    }

    fun saveImages(ctx: Context, pages: List<Bitmap>, baseName: String, fmt: SaveFormat): List<Uri> {
        val out = mutableListOf<Uri>()
        val s = stamp()
        pages.forEachIndexed { i, bmp ->
            val name = if (pages.size == 1) "$baseName-$s.${fmt.ext}"
            else "$baseName-$s-%02d.${fmt.ext}".format(i + 1)
            val sink = openImageSink(ctx, name, fmt) ?: return@forEachIndexed
            sink.second.use {
                bmp.compress(
                    if (fmt == SaveFormat.PNG) Bitmap.CompressFormat.PNG else Bitmap.CompressFormat.JPEG,
                    92, it
                )
            }
            finish(ctx, sink.first)
            out += sink.first
        }
        return out
    }

    /**
     * One PDF, one page per image.
     *
     * Written by [PdfWriter] rather than Android's PdfDocument: that draws into
     * a content stream and stores zlib-compressed raw pixels, which is several
     * megabytes for a scanned A4. Embedding a JPEG is a fraction of that.
     */
    fun savePdf(ctx: Context, pages: List<Bitmap>, baseName: String,
                quality: String = PdfWriter.DEFAULT_QUALITY): Uri? {
        val q = PdfWriter.QUALITY[quality] ?: PdfWriter.QUALITY[PdfWriter.DEFAULT_QUALITY]
        val doc = java.io.ByteArrayOutputStream()
        PdfWriter.write(doc, pages, 300, if (quality == "max") null else q)
        val name = "$baseName-${stamp()}.pdf"
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                    put(MediaStore.MediaColumns.MIME_TYPE, SaveFormat.PDF.mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH, "${Environment.DIRECTORY_DOCUMENTS}/$ALBUM")
                }
                val uri = ctx.contentResolver.insert(MediaStore.Files.getContentUri("external"), values)
                uri?.also { u -> ctx.contentResolver.openOutputStream(u)?.use { it.write(doc.toByteArray()) } }
            } else {
                val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS), ALBUM)
                dir.mkdirs()
                val f = File(dir, name)
                FileOutputStream(f).use { it.write(doc.toByteArray()) }
                FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", f)
            }
        } catch (_: Exception) {
            null
        }
    }

    fun share(ctx: Context, uris: List<Uri>, mime: String) {
        if (uris.isEmpty()) return
        val intent = if (uris.size == 1) {
            Intent(Intent.ACTION_SEND).apply {
                type = mime
                putExtra(Intent.EXTRA_STREAM, uris.first())
            }
        } else {
            Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                type = mime
                putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
            }
        }
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        ctx.startActivity(Intent.createChooser(intent, "Share scan").apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }
}
