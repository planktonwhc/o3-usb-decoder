package com.o3re.aoasniffer;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbAccessory;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.util.Log;
import android.widget.TextView;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Interactive AOA/DUML probe for the DJI Goggles 2 (see docs/protocol_notes.md).
 *
 * Registers as an alternate handler for the same AOA accessory DJI Fly uses
 * (manufacturer=DJI, model=com.dji.logiclink). Once it holds the accessory pipe it:
 *   - dumps every raw byte to a timestamped file under getExternalFilesDir(),
 *   - reassembles the outer AOA frames (55 CC 30 75 + LE len + DUML), parses inner DUML,
 *     and logs RESPONSE packets + flags any large (video-scale) frame,
 *   - reports a rolling byte-rate so a bandwidth jump is obvious,
 *   - accepts commands to send WITHOUT a rebuild, via `adb shell am broadcast`,
 *   - optionally forwards the raw stream over TCP for live `ffplay` on a host.
 *
 * Only one app can hold the accessory fd at a time, so DJI Fly gets no video while this runs.
 *
 * Send a built DUML command (app frames + CRCs + assigns seq):
 *   adb shell am broadcast -a com.o3re.aoasniffer.CMD --es cmdset 0x01 --es cmdid 0x21 \
 *       [--es payload 00] [--es receiver 0x01] [--es sender 0x02] [--es ack 2] [--es pack 0]
 * Send fully raw bytes verbatim (already framed):
 *   adb shell am broadcast -a com.o3re.aoasniffer.CMD --es raw 55cc3075...
 * Start TCP forward of the raw stream (then: adb forward tcp:9000 tcp:9000; nc/ffplay):
 *   adb shell am broadcast -a com.o3re.aoasniffer.FORWARD --es port 9000
 */
public class MainActivity extends Activity {
    private static final String TAG = "AoaSniffer";
    private static final String ACTION_USB_PERMISSION = "com.o3re.aoasniffer.USB_PERMISSION";
    private static final String ACTION_CMD = "com.o3re.aoasniffer.CMD";
    private static final String ACTION_FORWARD = "com.o3re.aoasniffer.FORWARD";

    private static final byte[] AOA_MAGIC = {0x55, (byte) 0xCC, 0x30, 0x75};
    private static final int LARGE_FRAME_THRESHOLD = 1024; // bytes; DUML telemetry is <100B

    private UsbManager usbManager;
    private ParcelFileDescriptor fd;
    private FileInputStream inputStream;
    private FileOutputStream accessoryOut;
    private FileOutputStream rawOut;
    private TextView statusView;
    private volatile boolean running = false;
    private long totalBytes = 0;
    private int outSeq = 1;

    // rolling rate
    private long rateWindowStart = 0;
    private long rateWindowBytes = 0;

    // TCP forward
    private volatile OutputStream tcpClientOut = null;
    private ServerSocket serverSocket = null;

    // frame reassembly buffer
    private byte[] acc = new byte[1 << 20];
    private int accLen = 0;

    // ================= DUML codec (mirrors analysis/duml.py) =================
    private static final int[] CRC8_TABLE = {
        0x00,0x5E,0xBC,0xE2,0x61,0x3F,0xDD,0x83,0xC2,0x9C,0x7E,0x20,0xA3,0xFD,0x1F,0x41,
        0x9D,0xC3,0x21,0x7F,0xFC,0xA2,0x40,0x1E,0x5F,0x01,0xE3,0xBD,0x3E,0x60,0x82,0xDC,
        0x23,0x7D,0x9F,0xC1,0x42,0x1C,0xFE,0xA0,0xE1,0xBF,0x5D,0x03,0x80,0xDE,0x3C,0x62,
        0xBE,0xE0,0x02,0x5C,0xDF,0x81,0x63,0x3D,0x7C,0x22,0xC0,0x9E,0x1D,0x43,0xA1,0xFF,
        0x46,0x18,0xFA,0xA4,0x27,0x79,0x9B,0xC5,0x84,0xDA,0x38,0x66,0xE5,0xBB,0x59,0x07,
        0xDB,0x85,0x67,0x39,0xBA,0xE4,0x06,0x58,0x19,0x47,0xA5,0xFB,0x78,0x26,0xC4,0x9A,
        0x65,0x3B,0xD9,0x87,0x04,0x5A,0xB8,0xE6,0xA7,0xF9,0x1B,0x45,0xC6,0x98,0x7A,0x24,
        0xF8,0xA6,0x44,0x1A,0x99,0xC7,0x25,0x7B,0x3A,0x64,0x86,0xD8,0x5B,0x05,0xE7,0xB9,
        0x8C,0xD2,0x30,0x6E,0xED,0xB3,0x51,0x0F,0x4E,0x10,0xF2,0xAC,0x2F,0x71,0x93,0xCD,
        0x11,0x4F,0xAD,0xF3,0x70,0x2E,0xCC,0x92,0xD3,0x8D,0x6F,0x31,0xB2,0xEC,0x0E,0x50,
        0xAF,0xF1,0x13,0x4D,0xCE,0x90,0x72,0x2C,0x6D,0x33,0xD1,0x8F,0x0C,0x52,0xB0,0xEE,
        0x32,0x6C,0x8E,0xD0,0x53,0x0D,0xEF,0xB1,0xF0,0xAE,0x4C,0x12,0x91,0xCF,0x2D,0x73,
        0xCA,0x94,0x76,0x28,0xAB,0xF5,0x17,0x49,0x08,0x56,0xB4,0xEA,0x69,0x37,0xD5,0x8B,
        0x57,0x09,0xEB,0xB5,0x36,0x68,0x8A,0xD4,0x95,0xCB,0x29,0x77,0xF4,0xAA,0x48,0x16,
        0xE9,0xB7,0x55,0x0B,0x88,0xD6,0x34,0x6A,0x2B,0x75,0x97,0xC9,0x4A,0x14,0xF6,0xA8,
        0x74,0x2A,0xC8,0x96,0x15,0x4B,0xA9,0xF7,0xB6,0xE8,0x0A,0x54,0xD7,0x89,0x6B,0x35,
    };
    private static final int[] CRC16_TABLE = {
        0x0000,0x1189,0x2312,0x329b,0x4624,0x57ad,0x6536,0x74bf,0x8c48,0x9dc1,0xaf5a,0xbed3,0xca6c,0xdbe5,0xe97e,0xf8f7,
        0x1081,0x0108,0x3393,0x221a,0x56a5,0x472c,0x75b7,0x643e,0x9cc9,0x8d40,0xbfdb,0xae52,0xdaed,0xcb64,0xf9ff,0xe876,
        0x2102,0x308b,0x0210,0x1399,0x6726,0x76af,0x4434,0x55bd,0xad4a,0xbcc3,0x8e58,0x9fd1,0xeb6e,0xfae7,0xc87c,0xd9f5,
        0x3183,0x200a,0x1291,0x0318,0x77a7,0x662e,0x54b5,0x453c,0xbdcb,0xac42,0x9ed9,0x8f50,0xfbef,0xea66,0xd8fd,0xc974,
        0x4204,0x538d,0x6116,0x709f,0x0420,0x15a9,0x2732,0x36bb,0xce4c,0xdfc5,0xed5e,0xfcd7,0x8868,0x99e1,0xab7a,0xbaf3,
        0x5285,0x430c,0x7197,0x601e,0x14a1,0x0528,0x37b3,0x263a,0xdecd,0xcf44,0xfddf,0xec56,0x98e9,0x8960,0xbbfb,0xaa72,
        0x6306,0x728f,0x4014,0x519d,0x2522,0x34ab,0x0630,0x17b9,0xef4e,0xfec7,0xcc5c,0xddd5,0xa96a,0xb8e3,0x8a78,0x9bf1,
        0x7387,0x620e,0x5095,0x411c,0x35a3,0x242a,0x16b1,0x0738,0xffcf,0xee46,0xdcdd,0xcd54,0xb9eb,0xa862,0x9af9,0x8b70,
        0x8408,0x9581,0xa71a,0xb693,0xc22c,0xd3a5,0xe13e,0xf0b7,0x0840,0x19c9,0x2b52,0x3adb,0x4e64,0x5fed,0x6d76,0x7cff,
        0x9489,0x8500,0xb79b,0xa612,0xd2ad,0xc324,0xf1bf,0xe036,0x18c1,0x0948,0x3bd3,0x2a5a,0x5ee5,0x4f6c,0x7df7,0x6c7e,
        0xa50a,0xb483,0x8618,0x9791,0xe32e,0xf2a7,0xc03c,0xd1b5,0x2942,0x38cb,0x0a50,0x1bd9,0x6f66,0x7eef,0x4c74,0x5dfd,
        0xb58b,0xa402,0x9699,0x8710,0xf3af,0xe226,0xd0bd,0xc134,0x39c3,0x284a,0x1ad1,0x0b58,0x7fe7,0x6e6e,0x5cf5,0x4d7c,
        0xc60c,0xd785,0xe51e,0xf497,0x8028,0x91a1,0xa33a,0xb2b3,0x4a44,0x5bcd,0x6956,0x78df,0x0c60,0x1de9,0x2f72,0x3efb,
        0xd68d,0xc704,0xf59f,0xe416,0x90a9,0x8120,0xb3bb,0xa232,0x5ac5,0x4b4c,0x79d7,0x685e,0x1ce1,0x0d68,0x3ff3,0x2e7a,
        0xe70e,0xf687,0xc41c,0xd595,0xa12a,0xb0a3,0x8238,0x93b1,0x6b46,0x7acf,0x4854,0x59dd,0x2d62,0x3ceb,0x0e70,0x1ff9,
        0xf78f,0xe606,0xd49d,0xc514,0xb1ab,0xa022,0x92b9,0x8330,0x7bc7,0x6a4e,0x58d5,0x495c,0x3de3,0x2c6a,0x1ef1,0x0f78,
    };

    private static int crc8(byte[] d, int off, int len) {
        int c = 0x77;
        for (int i = 0; i < len; i++) c = CRC8_TABLE[(d[off + i] ^ c) & 0xFF];
        return c;
    }
    private static int crc16(byte[] d, int off, int len) {
        int v = 0x3692;
        for (int i = 0; i < len; i++) v = (v >> 8) ^ CRC16_TABLE[(d[off + i] ^ v) & 0xFF];
        return v;
    }

    private static byte[] buildDuml(int senderType, int senderIdx, int receiverType, int receiverIdx,
                                    int seq, int packetType, int ackType, int encType,
                                    int cmdSet, int cmdId, byte[] payload) {
        int wholeLen = 11 + payload.length + 2;
        int verLen = (1 << 10) | (wholeLen & 0x3FF);
        byte[] out = new byte[wholeLen];
        out[0] = 0x55;
        out[1] = (byte) (verLen & 0xFF);
        out[2] = (byte) ((verLen >> 8) & 0xFF);
        out[4] = (byte) ((senderType & 0x1F) | ((senderIdx & 7) << 5));
        out[5] = (byte) ((receiverType & 0x1F) | ((receiverIdx & 7) << 5));
        out[6] = (byte) (seq & 0xFF);
        out[7] = (byte) ((seq >> 8) & 0xFF);
        out[8] = (byte) (((packetType & 1) << 7) | ((ackType & 3) << 5) | (encType & 7));
        out[9] = (byte) cmdSet;
        out[10] = (byte) cmdId;
        System.arraycopy(payload, 0, out, 11, payload.length);
        out[3] = (byte) crc8(out, 0, 3);
        int c16 = crc16(out, 0, wholeLen - 2);
        out[wholeLen - 2] = (byte) (c16 & 0xFF);
        out[wholeLen - 1] = (byte) ((c16 >> 8) & 0xFF);
        return out;
    }

    private static byte[] wrapAoaFrame(byte[] duml) {
        byte[] out = new byte[8 + duml.length];
        out[0] = 0x55; out[1] = (byte) 0xCC; out[2] = 0x30; out[3] = 0x75;
        out[4] = (byte) (duml.length & 0xFF);
        out[5] = (byte) ((duml.length >> 8) & 0xFF);
        out[6] = (byte) ((duml.length >> 16) & 0xFF);
        out[7] = (byte) ((duml.length >> 24) & 0xFF);
        System.arraycopy(duml, 0, out, 8, duml.length);
        return out;
    }

    private static byte[] hexToBytes(String s) {
        if (s == null) return new byte[0];
        s = s.replaceAll("[^0-9a-fA-F]", "");
        if (s.length() % 2 != 0) throw new IllegalArgumentException("odd hex length");
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++)
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        return out;
    }
    private static String bytesToHex(byte[] b, int off, int len) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < len; i++) sb.append(String.format(Locale.US, "%02x ", b[off + i]));
        return sb.toString();
    }

    // ================= lifecycle =================
    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (ACTION_USB_PERMISSION.equals(action)) {
                UsbAccessory a = intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY);
                if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false) && a != null) {
                    openAccessory(a);
                } else {
                    setStatus("Permission denied for accessory");
                }
            } else if (UsbManager.ACTION_USB_ACCESSORY_DETACHED.equals(action)) {
                setStatus("Accessory detached. Total bytes: " + totalBytes);
                closeAccessory();
            }
        }
    };

    private final BroadcastReceiver cmdReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (ACTION_CMD.equals(action)) {
                handleCmdBroadcast(intent);
            } else if (ACTION_FORWARD.equals(action)) {
                int port = parseIntExtra(intent, "port", 9000);
                startTcpForward(port);
            }
        }
    };

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        statusView = new TextView(this);
        statusView.setTextSize(14);
        statusView.setPadding(24, 48, 24, 24);
        setContentView(statusView);
        setStatus("Starting...");

        usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);

        IntentFilter usbF = new IntentFilter(ACTION_USB_PERMISSION);
        usbF.addAction(UsbManager.ACTION_USB_ACCESSORY_DETACHED);
        IntentFilter cmdF = new IntentFilter(ACTION_CMD);
        cmdF.addAction(ACTION_FORWARD);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbReceiver, usbF, Context.RECEIVER_EXPORTED);
            registerReceiver(cmdReceiver, cmdF, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(usbReceiver, usbF);
            registerReceiver(cmdReceiver, cmdF);
        }
        handleIntent(getIntent());
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        UsbAccessory accessory = null;
        if (intent != null && UsbManager.ACTION_USB_ACCESSORY_ATTACHED.equals(intent.getAction())) {
            accessory = intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY);
        }
        if (accessory == null) {
            UsbAccessory[] list = usbManager.getAccessoryList();
            if (list != null && list.length > 0) accessory = list[0];
        }
        if (accessory == null) {
            setStatus("No AOA accessory attached yet.\nPlug in the goggles and pick this app.");
            return;
        }
        if (usbManager.hasPermission(accessory)) {
            openAccessory(accessory);
        } else {
            setStatus("Requesting permission:\n" + describe(accessory));
            int flags = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
            PendingIntent pi = PendingIntent.getBroadcast(this, 0, new Intent(ACTION_USB_PERMISSION), flags);
            usbManager.requestPermission(accessory, pi);
        }
    }

    private void openAccessory(UsbAccessory accessory) {
        if (running) return;
        try {
            fd = usbManager.openAccessory(accessory);
            if (fd == null) { setStatus("openAccessory() returned null"); return; }
            inputStream = new FileInputStream(fd.getFileDescriptor());
            accessoryOut = new FileOutputStream(fd.getFileDescriptor());

            String ts = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
            File outFile = new File(getExternalFilesDir(null), "aoa_capture_" + ts + ".bin");
            rawOut = new FileOutputStream(outFile);

            running = true;
            totalBytes = 0;
            accLen = 0;
            Log.i(TAG, "Opened accessory: " + describe(accessory));
            Log.i(TAG, "Writing capture to " + outFile.getAbsolutePath());
            setStatus("READY. Capturing to:\n" + outFile.getName()
                    + "\n\nSend commands via `adb shell am broadcast -a "
                    + ACTION_CMD + " --es cmdset 0x.. --es cmdid 0x..`\nBytes: 0");

            new Thread(this::readLoop, "aoa-reader").start();
        } catch (Exception e) {
            Log.e(TAG, "Failed to open accessory", e);
            setStatus("Error opening accessory: " + e);
        }
    }

    private void readLoop() {
        byte[] buf = new byte[65536];
        try {
            while (running) {
                int n = inputStream.read(buf);
                if (n < 0) { Log.i(TAG, "Accessory EOF"); break; }
                if (n == 0) continue;

                rawOut.write(buf, 0, n);
                rawOut.flush();
                totalBytes += n;
                trackRate(n);

                OutputStream tcp = tcpClientOut;
                if (tcp != null) {
                    try { tcp.write(buf, 0, n); } catch (Exception e) { tcpClientOut = null; }
                }
                feedFrames(buf, n);
            }
        } catch (Exception e) {
            if (running) Log.e(TAG, "Read loop error", e);
        } finally {
            running = false;
            setStatus("Stopped. Total bytes: " + totalBytes);
        }
    }

    private void trackRate(int n) {
        long now = System.currentTimeMillis();
        if (rateWindowStart == 0) rateWindowStart = now;
        rateWindowBytes += n;
        if (now - rateWindowStart >= 1000) {
            long kbps = (rateWindowBytes * 8) / (now - rateWindowStart);
            Log.i(TAG, "rate: " + kbps + " kbit/s, total=" + totalBytes);
            setStatus("Capturing. rate=" + kbps + " kbit/s  total=" + totalBytes
                    + (tcpClientOut != null ? "\nTCP forward: ACTIVE" : ""));
            rateWindowStart = now;
            rateWindowBytes = 0;
        }
    }

    // Reassemble AOA frames (55 CC 30 75 + LE32 len + DUML) across chunk boundaries.
    private void feedFrames(byte[] chunk, int n) {
        if (accLen + n > acc.length) {
            // grow (or reset if runaway)
            int need = accLen + n;
            if (need > (16 << 20)) { accLen = 0; return; }
            byte[] bigger = new byte[Math.max(acc.length * 2, need)];
            System.arraycopy(acc, 0, bigger, 0, accLen);
            acc = bigger;
        }
        System.arraycopy(chunk, 0, acc, accLen, n);
        accLen += n;

        int pos = 0;
        while (true) {
            // find magic at pos
            int m = indexOfMagic(acc, pos, accLen);
            if (m < 0) { // no magic; keep last 3 bytes in case of split magic
                int keep = Math.min(3, accLen);
                System.arraycopy(acc, accLen - keep, acc, 0, keep);
                accLen = keep;
                return;
            }
            if (m + 8 > accLen) { compact(m); return; }
            int len = (acc[m + 4] & 0xFF) | ((acc[m + 5] & 0xFF) << 8)
                    | ((acc[m + 6] & 0xFF) << 16) | ((acc[m + 7] & 0xFF) << 24);
            if (len < 0 || len > (16 << 20)) { pos = m + 4; continue; } // bogus; resync
            if (m + 8 + len > accLen) { compact(m); return; } // wait for rest
            handleFrame(acc, m + 8, len);
            pos = m + 8 + len;
        }
    }

    private void compact(int from) {
        int keep = accLen - from;
        System.arraycopy(acc, from, acc, 0, keep);
        accLen = keep;
    }

    private static int indexOfMagic(byte[] a, int from, int end) {
        for (int i = from; i <= end - 4; i++) {
            if (a[i] == 0x55 && (a[i+1] & 0xFF) == 0xCC && a[i+2] == 0x30 && a[i+3] == 0x75) return i;
        }
        return -1;
    }

    private void handleFrame(byte[] a, int off, int len) {
        if (len >= LARGE_FRAME_THRESHOLD) {
            Log.w(TAG, "*** LARGE FRAME (possible VIDEO): " + len + " bytes, head="
                    + bytesToHex(a, off, Math.min(len, 16)));
            return;
        }
        // parse inner DUML minimally
        if (len >= 11 && (a[off] & 0xFF) == 0x55) {
            int senderType = a[off + 4] & 0x1F;
            int receiverType = a[off + 5] & 0x1F;
            int seq = (a[off + 6] & 0xFF) | ((a[off + 7] & 0xFF) << 8);
            int cmdType = a[off + 8] & 0xFF;
            int packetType = cmdType >> 7;
            int cmdSet = a[off + 9] & 0xFF;
            int cmdId = a[off + 10] & 0xFF;
            if (packetType == 1) { // RESPONSE — most interesting for probing
                Log.i(TAG, "RESP seq=" + seq + " src=" + senderType + " dst=" + receiverType
                        + " cmdset=0x" + Integer.toHexString(cmdSet)
                        + " cmdid=0x" + Integer.toHexString(cmdId)
                        + " payload=" + bytesToHex(a, off + 11, Math.max(0, len - 13)));
            }
        }
    }

    // ================= command sending =================
    private void handleCmdBroadcast(Intent intent) {
        try {
            String raw = intent.getStringExtra("raw");
            byte[] framed;
            String desc;
            if (raw != null && !raw.trim().isEmpty()) {
                framed = hexToBytes(raw);
                desc = "raw " + raw;
            } else {
                int cmdset = parseIntExtra(intent, "cmdset", -1);
                int cmdid = parseIntExtra(intent, "cmdid", -1);
                if (cmdset < 0 || cmdid < 0) { Log.e(TAG, "CMD missing cmdset/cmdid"); return; }
                int sender = parseIntExtra(intent, "sender", 2);   // MOBILE_APP
                int receiver = parseIntExtra(intent, "receiver", 1); // CAMERA
                int ack = parseIntExtra(intent, "ack", 2);         // ACK_AFTER_EXEC
                int pack = parseIntExtra(intent, "pack", 0);       // REQUEST
                byte[] payload = hexToBytes(intent.getStringExtra("payload"));
                byte[] duml = buildDuml(sender, 0, receiver, 0, outSeq++, pack, ack, 0,
                        cmdset, cmdid, payload);
                framed = wrapAoaFrame(duml);
                desc = "cmdset=0x" + Integer.toHexString(cmdset) + " cmdid=0x" + Integer.toHexString(cmdid)
                        + " recv=" + receiver + " payload=[" + bytesToHex(payload, 0, payload.length) + "]";
            }
            OutputStream out = accessoryOut;
            if (out == null) { Log.e(TAG, "not connected; cannot send"); return; }
            Log.i(TAG, "SEND " + desc + "  ->  " + bytesToHex(framed, 0, framed.length));
            out.write(framed);
            out.flush();
        } catch (Exception e) {
            Log.e(TAG, "handleCmdBroadcast failed", e);
        }
    }

    private static int parseIntExtra(Intent intent, String key, int def) {
        String s = intent.getStringExtra(key);
        if (s != null) {
            try { return Integer.decode(s.trim()); } catch (Exception e) { return def; }
        }
        return intent.getIntExtra(key, def);
    }

    // ================= TCP forward =================
    private void startTcpForward(final int port) {
        new Thread(() -> {
            try {
                if (serverSocket != null) { try { serverSocket.close(); } catch (Exception ignored) {} }
                serverSocket = new ServerSocket(port);
                Log.i(TAG, "TCP forward listening on " + port + " (adb forward tcp:" + port + " tcp:" + port + ")");
                Socket client = serverSocket.accept();
                tcpClientOut = client.getOutputStream();
                Log.i(TAG, "TCP forward client connected");
            } catch (Exception e) {
                Log.e(TAG, "TCP forward error", e);
            }
        }, "aoa-tcp").start();
    }

    // ================= misc =================
    private void closeAccessory() {
        running = false;
        try { if (inputStream != null) inputStream.close(); } catch (Exception ignored) {}
        try { if (accessoryOut != null) accessoryOut.close(); } catch (Exception ignored) {}
        try { if (rawOut != null) rawOut.close(); } catch (Exception ignored) {}
        try { if (fd != null) fd.close(); } catch (Exception ignored) {}
        try { if (serverSocket != null) serverSocket.close(); } catch (Exception ignored) {}
        inputStream = null; accessoryOut = null; rawOut = null; fd = null;
    }

    private static String describe(UsbAccessory a) {
        return "manufacturer=" + a.getManufacturer() + " model=" + a.getModel()
                + " description=" + a.getDescription() + " version=" + a.getVersion();
    }

    private void setStatus(String s) {
        Log.i(TAG, s);
        runOnUiThread(() -> statusView.setText(s));
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        try { unregisterReceiver(usbReceiver); } catch (Exception ignored) {}
        try { unregisterReceiver(cmdReceiver); } catch (Exception ignored) {}
        closeAccessory();
    }
}
