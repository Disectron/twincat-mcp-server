// SPDX-License-Identifier: MIT
/**
   Copyright (c) 2015 - 2022 Beckhoff Automation GmbH & Co. KG
 */

#include "NotificationDispatcher.h"
#include "Log.h"
#include <future>

NotificationDispatcher::NotificationDispatcher(
	DeleteNotificationCallback callback)
	: deleteNotification(callback)
	, ring(4 * 1024 * 1024)
	, stopExecution(false)
	, thread(&NotificationDispatcher::Run, this)
{
}

NotificationDispatcher::~NotificationDispatcher()
{
	stopExecution = true;
	sem.release();
	thread.join();
}

void NotificationDispatcher::Emplace(uint32_t hNotify,
				     std::shared_ptr<Notification> notification)
{
	std::lock_guard<std::recursive_mutex> lock(mutex);
	notifications.emplace(hNotify, notification);
}

long NotificationDispatcher::Erase(uint32_t hNotify, uint32_t tmms)
{
	const auto status = deleteNotification(hNotify, tmms);
	std::lock_guard<std::recursive_mutex> lock(mutex);
	notifications.erase(hNotify);
	return status;
}

std::shared_ptr<Notification> NotificationDispatcher::Find(uint32_t hNotify)
{
	std::lock_guard<std::recursive_mutex> lock(mutex);
	auto it = notifications.find(hNotify);
	if (it != notifications.end()) {
		return it->second;
	}
	return {};
}

void NotificationDispatcher::Notify()
{
	sem.release();
}

void NotificationDispatcher::Run()
{
	for (;;) {
		sem.acquire();
		if (stopExecution) {
			return;
		}
		// We wrote the fullLength ourself in AmsConnection::ReceiveNotification()
		auto fullLength = ring.ReadFromLittleEndian<uint32_t>();

		/** Every bound below is taken from fullLength, never from the
		 * ring itself, so the two have to agree. If they do not, our
		 * own bookkeeping drifted and the ring no longer holds what we
		 * think it does. The counting semaphore leaves us no way to
		 * drain and resync, so stop parsing instead.
		 */
		if (fullLength > ring.BytesAvailable()) {
			LOG_ERROR("Notification length "
				  << std::dec << fullLength
				  << " exceeds the ring content: " << std::dec
				  << ring.BytesAvailable());
			return;
		}

		/** fullLength counts the payload only, AmsConnection wrote it
		 * next to the payload and not as part of it. So the shortest
		 * well formed stream is its own length plus a stamp count.
		 */
		if (fullLength < 2 * sizeof(uint32_t)) {
			LOG_WARN("Notification length too short: "
				 << std::dec << fullLength);
			ring.Read(fullLength);
			continue;
		}

		/** From here on fullLength is what is left in the ring for this
		 * notification, so every skip below is exact. Subtracting
		 * before the check above would wrap it for a runt frame.
		 */
		fullLength -= sizeof(uint32_t);

		// The stream repeats its length, counted from behind that field
		const auto length = ring.ReadFromLittleEndian<uint32_t>();
		if (length != fullLength) {
			LOG_WARN("Notification length mismatch: "
				 << std::dec << length << " != " << std::dec
				 << fullLength);
			ring.Read(fullLength);
			continue;
		}

		auto numStamps = ring.ReadFromLittleEndian<uint32_t>();
		fullLength -= sizeof(numStamps);
		while (numStamps-- > 0) {
#pragma pack(push, 1)
			struct {
				uint64_t timestamp;
				uint32_t numSamples;
			} stamp;
#pragma pack(pop)
			if (sizeof(stamp) > fullLength) {
				LOG_WARN(
					"Notification too short for stamp header: "
					<< std::dec << fullLength);
				goto cleanup;
			}

			stamp.timestamp = ring.ReadFromLittleEndian<uint64_t>();
			stamp.numSamples =
				ring.ReadFromLittleEndian<uint32_t>();
			fullLength -= sizeof(stamp);
			while (stamp.numSamples-- > 0) {
#pragma pack(push, 1)
				struct {
					uint32_t hNotify;
					uint32_t size;
				} sample;
#pragma pack(pop)

				if (sizeof(sample) > fullLength) {
					LOG_WARN(
						"Notification too short for sample header: "
						<< std::dec << fullLength);
					goto cleanup;
				}
				sample.hNotify =
					ring.ReadFromLittleEndian<uint32_t>();
				sample.size =
					ring.ReadFromLittleEndian<uint32_t>();
				fullLength -= sizeof(sample);

				if (sample.size > fullLength) {
					LOG_WARN("Notification too short: "
						 << std::dec << fullLength
						 << " to hold sample data "
						 << std::dec << sample.size);
					goto cleanup;
				}
				const auto notification = Find(sample.hNotify);
				if (notification) {
					if (sample.size !=
					    notification->Size()) {
						LOG_WARN(
							"Notification sample size: "
							<< sample.size
							<< " doesn't match: "
							<< notification->Size());
						goto cleanup;
					}
					notification->Notify(stamp.timestamp,
							     ring);
				} else {
					LOG_WARN("Unhandled Notification: 0x"
						 << std::hex << sample.hNotify);
					ring.Read(sample.size);
				}
				fullLength -= sample.size;
			}
		}
cleanup:
		ring.Read(fullLength);
	}
}
