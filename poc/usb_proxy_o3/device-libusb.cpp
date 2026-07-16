#include <atomic>
#include <ctime>

#include "device-libusb.h"

libusb_device 			**devs;
libusb_device_handle 		*dev_handle;
libusb_context 			*context = NULL;
libusb_hotplug_callback_handle	callback_handle = -1;

struct libusb_device_descriptor		device_device_desc;
struct libusb_config_descriptor		**device_config_desc;

pthread_t hotplug_monitor_thread;
static bool hotplug_monitor_started = false;

// O3 natural-flow AOA re-enum state (see README + gofly_transition_findings.md).
bool follow_aoa_reenum = false;
std::atomic<bool> phone_device_left(false);
std::atomic<bool> aoa_reenum_needed(false);
bool aoa_reenum_done = false;

// VID/PID we currently have open (for hotplug DEVICE_LEFT matching).
static int open_vendor_id = -1;
static int open_product_id = -1;

static const uint16_t AOA_ACCESSORY_PIDS[] = {
	0x2d00, 0x2d01, 0x2d04, 0x2d05
};

bool is_aoa_accessory_vid_pid(uint16_t vid, uint16_t pid) {
	if (vid != AOA_GOOGLE_VID)
		return false;
	for (size_t i = 0; i < sizeof(AOA_ACCESSORY_PIDS) / sizeof(AOA_ACCESSORY_PIDS[0]); i++)
		if (pid == AOA_ACCESSORY_PIDS[i])
			return true;
	return false;
}

int hotplug_callback(struct libusb_context *ctx __attribute__((unused)),
			struct libusb_device *dev __attribute__((unused)),
			libusb_hotplug_event event __attribute__((unused)),
			void *user_data __attribute__((unused))) {
	// Natural-flow AOA: phone disconnects AFTER ACCESSORY_START so it can
	// re-enumerate as 18d1:2d0x. Do NOT kill the process (exiting without a
	// clean close of /dev/raw-gadget can oops the module — gotcha #4). Main
	// recycles the gadget once reconnect_as_aoa_accessory() succeeds.
	//
	// IMPORTANT: only treat DEVICE_LEFT as AOA re-enum if ACCESSORY_START
	// already set aoa_reenum_needed. A bare leave (claim-time re-enum of
	// MTP 04e8:6860→same PID, cable glitch) must NOT start a 30s wait for
	// 18d1:2d0x — that was a false positive on the first natural-flow run.
	if (follow_aoa_reenum && !aoa_reenum_done && aoa_reenum_needed.load()) {
		printf("Hotplug: phone DEVICE_LEFT after ACCESSORY_START "
			"(AOA re-enum follow — not exiting)\n");
		phone_device_left = true;
		please_stop_ep0 = true;
		please_stop_eps = true;
		return 0;
	}
	if (follow_aoa_reenum && !aoa_reenum_done) {
		// Unexpected leave before ACCESSORY_START (e.g. MTP re-enum on
		// claim). Log + mark left; do not recycle as accessory.
		printf("Hotplug: phone DEVICE_LEFT BEFORE ACCESSORY_START "
			"(not AOA — ignoring for re-enum follow; proxy may stall)\n");
		phone_device_left = true;
		return 0;
	}
	printf("Hotplug event: device disconnected, stopping proxy...\n");
	kill(0, SIGINT);
	return 0;
}

void *hotplug_monitor(void *arg __attribute__((unused))) {
	printf("Start hotplug_monitor/event thread, thread id(%d)\n", gettid());
	while (true) {
		// This is the SOLE thread that calls libusb_handle_events.
		// All other threads (ISO IN, ISO OUT) submit async transfers
		// and spin-wait on their completion flags.  This avoids event
		// lock contention that would otherwise starve ISO OUT sends.
		struct timeval tv = {1, 0};
		libusb_handle_events_timeout(context, &tv);
	}
}

void free_device_descriptors() {
	if (!device_config_desc)
		return;
	// bNumConfigurations is valid for the descriptors we free.
	int n = device_device_desc.bNumConfigurations;
	for (int i = 0; i < n; i++) {
		if (device_config_desc[i])
			libusb_free_config_descriptor(device_config_desc[i]);
	}
	delete[] device_config_desc;
	device_config_desc = nullptr;
}

void close_device_handle() {
	if (callback_handle != -1 && context) {
		libusb_hotplug_deregister_callback(context, callback_handle);
		callback_handle = -1;
	}
	if (dev_handle) {
		libusb_close(dev_handle);
		dev_handle = NULL;
	}
	free_device_descriptors();
	open_vendor_id = -1;
	open_product_id = -1;
}

int get_descriptor(libusb_device *device) {
	int result;
	result = libusb_get_device_descriptor(device, &device_device_desc);
	if (result != LIBUSB_SUCCESS) {
		if (verbose_level) {
			fprintf(stderr, "Error retrieving device descriptor: %s\n",
					libusb_strerror((libusb_error)result));
		}
		return result;
	}

	// Caller must free previous descriptors first if reusing.
	device_config_desc = new struct libusb_config_descriptor *[device_device_desc.bNumConfigurations];
	for (int i = 0; i < device_device_desc.bNumConfigurations; i++) {
		result = libusb_get_config_descriptor(device, i, &device_config_desc[i]);
		if (result != LIBUSB_SUCCESS) {
			if (verbose_level) {
				fprintf(stderr, "Error retrieving configuration(%d) descriptor: %s\n",
						i, libusb_strerror((libusb_error)result));
			}
			return result;
		}
	}

	return LIBUSB_SUCCESS;
}

static int ensure_libusb_context() {
	if (context)
		return 0;
	int result = libusb_init(&context);
	if (result < 0) {
		fprintf(stderr, "Init error: %s\n", libusb_strerror((libusb_error)result));
		return 1;
	}
	libusb_set_debug(context, 3);
	return 0;
}

static int ensure_hotplug_monitor() {
	if (hotplug_monitor_started)
		return 0;
	pthread_create(&hotplug_monitor_thread, 0, hotplug_monitor, nullptr);
	hotplug_monitor_started = true;
	return 0;
}

static int register_device_left_hotplug(int vendor_id, int product_id) {
	if (callback_handle != -1) {
		libusb_hotplug_deregister_callback(context, callback_handle);
		callback_handle = -1;
	}
	int result = libusb_hotplug_register_callback(context,
		(libusb_hotplug_event) (LIBUSB_HOTPLUG_EVENT_DEVICE_LEFT),
		(libusb_hotplug_flag) 0, vendor_id, product_id,
		LIBUSB_HOTPLUG_MATCH_ANY, hotplug_callback, NULL, &callback_handle);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Error registering hotplug callback\n");
		return result;
	}
	return 0;
}

// Open a device already found in the device list (and described in device_* globals).
static int open_found_device(libusb_device *found, int vendor_id, int product_id) {
	int result = libusb_open(found, &dev_handle);
	if (result != LIBUSB_SUCCESS) {
		if (verbose_level) {
			fprintf(stderr, "Error opening device handle: %s\n",
					libusb_strerror((libusb_error)result));
		}
		dev_handle = NULL;
		return result;
	}

	result = libusb_set_auto_detach_kernel_driver(dev_handle, 0);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "libusb_set_auto_detach_kernel_driver() failed: %s\n",
				libusb_strerror((libusb_error)result));
		return result;
	}

	int config = 0;
	result = libusb_get_configuration(dev_handle, &config);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "libusb_get_configuration() failed: %s\n",
				libusb_strerror((libusb_error)result));
		return result;
	}

	for (int i = 0; i < device_device_desc.bNumConfigurations; i++) {
		if (device_config_desc[i]->bConfigurationValue != config)
			continue;
		for (int j = 0; j < device_config_desc[i]->bNumInterfaces; j++)
			libusb_detach_kernel_driver(dev_handle, j);
	}

	if (reset_device_before_proxy) {
		result = libusb_reset_device(dev_handle);
		if (result != LIBUSB_SUCCESS) {
			fprintf(stderr, "libusb_reset_device() failed: %s\n",
					libusb_strerror((libusb_error)result));
			return result;
		}
	}

	//check that device is responsive
	unsigned char unused[4];
	result = libusb_get_string_descriptor(dev_handle, 0, 0, unused, sizeof(unused));
	if (result < 0) {
		fprintf(stderr, "Device unresponsive: %s\n",
				libusb_strerror((libusb_error)result));
		return result;
	}

	open_vendor_id = vendor_id;
	open_product_id = product_id;
	phone_device_left = false;

	if (register_device_left_hotplug(vendor_id, product_id) != 0)
		return -1;
	ensure_hotplug_monitor();
	return 0;
}

// Scan once for a matching device. If found, open it. Returns 0 on success.
// match_aoa_accessory: if true, ignore vid/pid args and match Google accessory PIDs.
static int try_find_and_open(int vendor_id, int product_id, bool match_aoa_accessory) {
	libusb_device *found = NULL;
	int cnt = libusb_get_device_list(context, &devs);
	if (cnt < 0) {
		fprintf(stderr, "Get Device Error: %s\n",
				libusb_strerror((libusb_error)cnt));
		return 1;
	}
	if (verbose_level)
		printf("%d Devices in list\n", cnt);

	for (int i = 0; i < cnt; i++) {
		libusb_device *dvc = devs[i];
		// free previous partial get_descriptor allocations if any
		free_device_descriptors();
		int result = get_descriptor(dvc);
		if (result != LIBUSB_SUCCESS)
			continue;

		if (device_device_desc.bDeviceClass == LIBUSB_CLASS_HUB)
			continue;

		bool match = false;
		if (match_aoa_accessory) {
			match = is_aoa_accessory_vid_pid(device_device_desc.idVendor,
							device_device_desc.idProduct);
		} else if (vendor_id == -1 && product_id == -1) {
			match = true;
		} else if ((vendor_id == device_device_desc.idVendor ||
			    vendor_id == LIBUSB_HOTPLUG_MATCH_ANY) &&
			   (product_id == device_device_desc.idProduct ||
			    product_id == LIBUSB_HOTPLUG_MATCH_ANY)) {
			match = true;
		}
		if (match) {
			found = dvc;
			break;
		}
	}

	if (!found) {
		if (verbose_level && (match_aoa_accessory ||
				      (vendor_id != -1 && product_id != -1)))
			printf("Target device not found\n");
		free_device_descriptors();
		libusb_free_device_list(devs, 1);
		return 1;
	}

	int vid = device_device_desc.idVendor;
	int pid = device_device_desc.idProduct;
	int result = open_found_device(found, vid, pid);
	libusb_free_device_list(devs, 1);
	if (result != 0) {
		close_device_handle();
		return result;
	}
	printf("Opened device %04x:%04x\n", vid, pid);
	return 0;
}

int connect_device(int vendor_id, int product_id) {
	if (ensure_libusb_context() != 0)
		return 1;

	while (true) {
		int rv = try_find_and_open(vendor_id, product_id, false);
		if (rv == 0)
			return 0;
		if (verbose_level && vendor_id != -1 && product_id != -1)
			printf("Target device not found\n");
		sleep(1);
	}
}

int reconnect_as_aoa_accessory(int timeout_sec) {
	printf("AOA re-enum: closing prior phone handle, waiting up to %ds for "
	       "18d1:2d0x accessory ...\n", timeout_sec);
	close_device_handle();
	phone_device_left = false;

	if (ensure_libusb_context() != 0)
		return 1;

	time_t deadline = time(NULL) + timeout_sec;
	while (time(NULL) <= deadline) {
		int rv = try_find_and_open(-1, -1, true);
		if (rv == 0) {
			printf("AOA re-enum: accessory open OK (%04x:%04x)\n",
				device_device_desc.idVendor, device_device_desc.idProduct);
			aoa_reenum_done = true;
			aoa_reenum_needed = false;
			phone_device_left = false;
			return 0;
		}
		usleep(200000); // 200 ms
	}
	fprintf(stderr, "AOA re-enum: timed out waiting for accessory device\n");
	return 1;
}

void reset_device() {
	if (!dev_handle)
		return;
	int result = libusb_reset_device(dev_handle);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Error resetting device: %s\n",
				libusb_strerror((libusb_error)result));
	}
}

void set_configuration(int configuration) {
	if (!dev_handle) {
		fprintf(stderr, "set_configuration: no device handle\n");
		return;
	}
	// O3 MITM fix: skip the SET_CONFIGURATION if the device is already in this
	// configuration. libusb_set_configuration() always issues a real SET_CONFIG
	// control transfer, which re-configures the device's endpoints — and on an
	// Android AOA-accessory phone that knocks it out of accessory mode back to MTP
	// (04e8:6860), breaking the bridge exactly when the goggles configure the gadget.
	int current = -1;
	if (libusb_get_configuration(dev_handle, &current) == LIBUSB_SUCCESS &&
			current == configuration) {
		printf("Device already in configuration %d; skipping SET_CONFIGURATION\n",
				configuration);
		return;
	}
	int result = libusb_set_configuration(dev_handle, configuration);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Error setting configuration(%d): %s\n",
				configuration, libusb_strerror((libusb_error)result));
	}
}

void claim_interface(int interface) {
	if (!dev_handle)
		return;
	int result = libusb_claim_interface(dev_handle, interface);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Error claiming interface(%d): %s\n",
				interface, libusb_strerror((libusb_error)result));
	}
}

void release_interface(int interface) {
	if (!dev_handle)
		return;
	int result = libusb_release_interface(dev_handle, interface);
	if (result != LIBUSB_SUCCESS && result != LIBUSB_ERROR_NOT_FOUND) {
		fprintf(stderr, "Error releasing interface(%d): %s\n",
				interface, libusb_strerror((libusb_error)result));
	}
}

void set_interface_alt_setting(int interface, int altsetting) {
	if (!dev_handle)
		return;
	int result = libusb_set_interface_alt_setting(dev_handle, interface, altsetting);
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Error setting interface altsetting(%d, %d): %s\n",
				interface, altsetting, libusb_strerror((libusb_error)result));
	}
}

int control_request(const usb_ctrlrequest *setup_packet, int *nbytes,
			unsigned char **dataptr, int timeout) {
	if (!dev_handle)
		return LIBUSB_ERROR_NO_DEVICE;
	int result = libusb_control_transfer(dev_handle,
					setup_packet->bRequestType, setup_packet->bRequest,
					setup_packet->wValue, setup_packet->wIndex, *dataptr,
					setup_packet->wLength, timeout);

	if (result < 0) {
		if (verbose_level) {
			fprintf(stderr, "Error sending setup packet: %s\n",
					libusb_strerror((libusb_error)result));
		}
		if (result == LIBUSB_ERROR_PIPE)
			return -1;
		return result;
	}
	else {
		if (verbose_level)
			printf("Control transfer succeed\n");
	}

	// Natural-flow: detect ACCESSORY_START (vendor OUT, bRequest=53). The phone
	// will disconnect and re-enumerate as Google accessory — request a recycle.
	if (follow_aoa_reenum && !aoa_reenum_done &&
	    (setup_packet->bRequestType & USB_TYPE_MASK) == USB_TYPE_VENDOR &&
	    !(setup_packet->bRequestType & USB_DIR_IN) &&
	    setup_packet->bRequest == AOA_START) {
		printf("O3: ACCESSORY_START delivered to phone — scheduling AOA re-enum follow\n");
		aoa_reenum_needed = true;
		// Give the phone a moment to drop the bus; hotplug will also fire.
		// Exit ep0 so main can close raw_gadget (goggles see disconnect) and
		// re-present accessory descriptors.
		please_stop_ep0 = true;
		please_stop_eps = true;
	}

	*nbytes = result;
	return 0;
}

int send_iso_data(uint8_t endpoint, uint8_t *dataptr, int length, int timeout);

int send_data(uint8_t endpoint, uint8_t attributes, uint8_t *dataptr,
			int length, int timeout) {
	int transferred;
	int attempt = 0;
	int result = LIBUSB_SUCCESS;

	bool incomplete_transfer = false;

	if (!dev_handle)
		return LIBUSB_ERROR_NO_DEVICE;

	switch (attributes & USB_ENDPOINT_XFERTYPE_MASK) {
	case USB_ENDPOINT_XFER_CONTROL:
		fprintf(stderr, "Can't send on a control endpoint.\n");
		break;
	case USB_ENDPOINT_XFER_BULK:
		do {
			result = libusb_bulk_transfer(dev_handle, endpoint, dataptr, length, &transferred, timeout);
			//TODO retry transfer if incomplete
			if (transferred != length) {
				fprintf(stderr, "Incomplete Bulk transfer on EP%02x for attempt %d. length(%d), transferred(%d)\n",
					endpoint, attempt, length, transferred);
				incomplete_transfer = true;
			}
			if (result == LIBUSB_SUCCESS) {
				if (incomplete_transfer)
					printf("Resent Bulk transfer on EP%02x for attempt %d. length(%d), transferred(%d)\n",
						endpoint, attempt, length, transferred);
				if (verbose_level > 2)
					printf("Sent %d bytes (Bulk) to EP%02x\n", transferred, endpoint);
			}
			if ((result == LIBUSB_ERROR_PIPE || result == LIBUSB_ERROR_TIMEOUT))
				libusb_clear_halt(dev_handle, endpoint);

			attempt++;
		} while ((result == LIBUSB_ERROR_PIPE || result == LIBUSB_ERROR_TIMEOUT || transferred != length)
					&& attempt < MAX_ATTEMPTS);
		break;
	case USB_ENDPOINT_XFER_INT:
		result = libusb_interrupt_transfer(dev_handle, endpoint, dataptr, length, &transferred, timeout);

		if (transferred != length)
			fprintf(stderr, "Incomplete Interrupt transfer on EP%02x\n", endpoint);
		if (result == LIBUSB_SUCCESS && verbose_level > 2)
			printf("Sent %d bytes (Int) to libusb EP%02x\n", transferred, endpoint);
		break;
	}
	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Transfer error sending on EP%02x: %s\n",
				endpoint, libusb_strerror((libusb_error)result));
	}
	return result;
}

void iso_transfer_callback(struct libusb_transfer *transfer) {
	int *iso_completed = (int *)transfer->user_data;
	*iso_completed = 1;
}

// Bounded async ISO OUT: submit and return immediately.
// The dedicated event thread (hotplug_monitor) processes completions.
// We limit in-flight transfers to avoid flooding the kernel.
#define ISO_OUT_MAX_IN_FLIGHT 8
static std::atomic<int> iso_out_in_flight(0);

static void iso_out_callback(struct libusb_transfer *transfer) {
	iso_out_in_flight--;
	if (transfer->status != LIBUSB_TRANSFER_COMPLETED) {
		static int iso_out_err_count = 0;
		iso_out_err_count++;
		if (iso_out_err_count <= 10 || iso_out_err_count % 100 == 0)
			fprintf(stderr, "ISO OUT EP%02x failed: status=%d (total errors: %d)\n",
				transfer->endpoint, transfer->status, iso_out_err_count);
	}
	// Free the buffer passed via user_data.
	delete[] (unsigned char *)transfer->user_data;
	libusb_free_transfer(transfer);
}

int send_iso_data(uint8_t endpoint, uint8_t *dataptr, int length, int timeout) {
	// If at capacity, wait briefly for a slot.
	int waited_us = 0;
	while (iso_out_in_flight >= ISO_OUT_MAX_IN_FLIGHT && waited_us < 2000) {
		usleep(50);
		waited_us += 50;
	}
	if (iso_out_in_flight >= ISO_OUT_MAX_IN_FLIGHT) {
		// Drop this packet -- ISO is inherently lossy.
		// Free the buffer since the callback won't run.
		delete[] dataptr;
		return LIBUSB_SUCCESS;
	}

	struct libusb_transfer *transfer = libusb_alloc_transfer(1);
	if (!transfer) {
		fprintf(stderr, "Failed to allocate libusb_transfer for ISO OUT.\n");
		return LIBUSB_ERROR_OTHER;
	}

	// The callback frees both the transfer and the buffer (via user_data).
	libusb_fill_iso_transfer(transfer, dev_handle, endpoint, dataptr, length,
				1, iso_out_callback, dataptr, timeout);
	libusb_set_iso_packet_lengths(transfer, length);

	int rv = libusb_submit_transfer(transfer);
	if (rv != LIBUSB_SUCCESS) {
		fprintf(stderr, "ISO OUT submit failed on EP%02x: %s (len=%d)\n",
			endpoint, libusb_strerror((libusb_error)rv), length);
		libusb_free_transfer(transfer);
		return rv;
	}

	iso_out_in_flight++;
	return LIBUSB_SUCCESS;
}

int receive_iso_data_batched(uint8_t endpoint, uint16_t maxPacketSize,
			struct iso_batch_result *result, int batch_size, int timeout) {
	if (batch_size < 1)
		batch_size = 1;
	if (batch_size > ISO_BATCH_SIZE_MAX)
		batch_size = ISO_BATCH_SIZE_MAX;

	memset(result, 0, sizeof(*result));
	result->buffer = new uint8_t[maxPacketSize * batch_size];

	struct libusb_transfer *transfer = libusb_alloc_transfer(batch_size);
	if (!transfer) {
		fprintf(stderr, "Failed to allocate libusb_transfer for ISO batch.\n");
		delete[] result->buffer;
		result->buffer = nullptr;
		return LIBUSB_ERROR_OTHER;
	}

	volatile int iso_completed = 0;
	libusb_fill_iso_transfer(transfer, dev_handle, endpoint, result->buffer,
				maxPacketSize * batch_size, batch_size,
				iso_transfer_callback, (void *)&iso_completed, timeout);
	libusb_set_iso_packet_lengths(transfer, maxPacketSize);

	int rv = libusb_submit_transfer(transfer);
	if (rv != LIBUSB_SUCCESS) {
		if (verbose_level)
			fprintf(stderr, "ISO batch submit failed on EP%02x: %s\n",
				endpoint, libusb_strerror((libusb_error)rv));
		libusb_free_transfer(transfer);
		delete[] result->buffer;
		result->buffer = nullptr;
		return rv;
	}

	// Spin-wait for completion; the dedicated event thread
	// (hotplug_monitor) will call iso_transfer_callback.
	while (!iso_completed)
		usleep(50);

	if (transfer->status != LIBUSB_TRANSFER_COMPLETED &&
	    transfer->status != LIBUSB_TRANSFER_TIMED_OUT) {
		if (verbose_level)
			fprintf(stderr, "ISO batch transfer failed on EP%02x: status %d\n",
				endpoint, transfer->status);
		if (transfer->status == LIBUSB_TRANSFER_STALL)
			libusb_clear_halt(dev_handle, endpoint);
		libusb_free_transfer(transfer);
		delete[] result->buffer;
		result->buffer = nullptr;
		return LIBUSB_ERROR_IO;
	}

	result->num_packets = batch_size;
	uint8_t *packet_ptr = result->buffer;
	for (int i = 0; i < batch_size; i++) {
		result->packets[i].data = packet_ptr;
		result->packets[i].actual_length = transfer->iso_packet_desc[i].actual_length;
		result->packets[i].status = transfer->iso_packet_desc[i].status;
		result->total_length += result->packets[i].actual_length;
		packet_ptr += maxPacketSize;

		if (result->packets[i].status == LIBUSB_TRANSFER_COMPLETED &&
		    result->packets[i].actual_length > 0)
			result->success = true;
	}

	if (verbose_level > 2)
		printf("ISO batch received: %d packets, %d total bytes\n",
			batch_size, result->total_length);

	libusb_free_transfer(transfer);
	return LIBUSB_SUCCESS;
}

int receive_data(uint8_t endpoint, uint8_t attributes, uint16_t maxPacketSize,
			uint8_t **dataptr, int *length, int timeout) {
	int result = LIBUSB_SUCCESS;

	int attempt = 0;
	if (!dev_handle)
		return LIBUSB_ERROR_NO_DEVICE;

	switch (attributes & USB_ENDPOINT_XFERTYPE_MASK) {
	case USB_ENDPOINT_XFER_CONTROL:
		fprintf(stderr, "Can't read on a control endpoint.\n");
		break;
	case USB_ENDPOINT_XFER_ISOC:
		// ISO IN is handled by receive_iso_data_batched() directly.
		fprintf(stderr, "receive_data() should not be called for ISO endpoints.\n");
		break;
	case USB_ENDPOINT_XFER_BULK:
		*dataptr = new uint8_t[maxPacketSize * 8];
		do {
			result = libusb_bulk_transfer(dev_handle, endpoint, *dataptr, maxPacketSize, length, timeout);
			if (result == LIBUSB_SUCCESS && verbose_level > 2)
				printf("Received bulk data(%d) bytes\n", *length);
			if ((result == LIBUSB_ERROR_PIPE || result == LIBUSB_ERROR_TIMEOUT))
				libusb_clear_halt(dev_handle, endpoint);

			attempt++;
		} while ((result == LIBUSB_ERROR_PIPE || result == LIBUSB_ERROR_TIMEOUT) && attempt < MAX_ATTEMPTS);
		break;
	case USB_ENDPOINT_XFER_INT:
		*dataptr = new uint8_t[maxPacketSize];
		result = libusb_interrupt_transfer(dev_handle, endpoint, *dataptr, maxPacketSize, length, timeout);
		if (result == LIBUSB_SUCCESS && verbose_level > 2)
			printf("Received int data(%d) bytes\n", *length);
		break;
	}

	if (result != LIBUSB_SUCCESS) {
		fprintf(stderr, "Transfer error receiving on EP%02x: %s\n",
				endpoint, libusb_strerror((libusb_error)result));
	}

	return result;
}
