// SPDX-License-Identifier: MIT
/**
   Copyright (C) Beckhoff Automation GmbH & Co. KG
 */

#include "SysServAccess.h"
#include "Log.h"
#include <chrono>
#include <stdexcept>
#include <thread>
#include <type_traits>

namespace bhf
{
namespace ads
{

#pragma pack(push, 1)
// Layout of TwinCAT's AdsSysServState. All multi-byte fields are transmitted in
// little endian. We keep them exactly as received (hence the 'le' prefix) and
// convert lazily in the accessors, just like the AoE headers in AmsHeader.h. The
// ADS and device state are 2 byte fields on the wire, so we must not use the
// ADSSTATE enum here: its underlying size depends on the headers/toolchain in
// use. Fields we don't read are kept to preserve the wire layout.
struct AdsSysServState {
	ADSSTATE ads() const
	{
		return toState(leAdsState);
	}

	ADSSTATE device() const
	{
		return toState(leDeviceState);
	}

	// The restartCounter increments by one after each start/restart (and is
	// never 0 if supported). It occupies bits 8..11 of the flags field.
	uint8_t restartCounter() const
	{
		return (bhf::ads::letoh(leFlags) >> 8) & 0xF;
	}

	uint16_t leAdsState;
	uint16_t leDeviceState;
	uint16_t leRestartIndex;
	uint8_t version;
	uint8_t revision;
	uint16_t leBuild;
	uint8_t platform;
	uint8_t osType;
	uint16_t leFlags;
	uint16_t reserved;

    private:
	// Validate and convert a little endian wire field into an ADSSTATE.
	static ADSSTATE toState(const uint16_t leState)
	{
		const auto state = bhf::ads::letoh(leState);
		if (state >= ADSSTATE::ADSSTATE_MAXSTATES) {
			throw std::out_of_range("Unknown ADSSTATE(" +
						std::to_string(state) + ')');
		}
		return static_cast<ADSSTATE>(state);
	}
};
#pragma pack(pop)
static_assert(sizeof(AdsSysServState) == 16,
	      "unexpected size of AdsSysServState");
static_assert(std::is_trivially_copyable<AdsSysServState>::value,
	      "AdsSysServState must be trivially copyable");

static long ReadSysServState(const AdsDevice &device, AdsSysServState &state)
{
	uint32_t bytesRead = 0;
	const auto status = device.ReadReqEx2(SYSTEMSERVICE_SYSSERV_STATE, 0,
					      sizeof(state), &state,
					      &bytesRead);

	if (ADSERR_NOERR == status && bytesRead != sizeof(state)) {
		LOG_ERROR(__FUNCTION__ << "(): ReadSysServState() read "
				       << bytesRead << " bytes, expected "
				       << sizeof(state) << '\n');
		return ADSERR_DEVICE_INVALIDDATA;
	}

	return status;
}

SysServAccess::SysServAccess(const std::string &gw, const AmsNetId netid,
			     const uint16_t port)
	: device(gw, netid, port ? port : 10000)
{
}

long SysServAccess::Reconfig(const uint32_t timeout) const
{
	// Read the current restartCounter.
	AdsSysServState oldState{};
	const auto status = ReadSysServState(device, oldState);
	if (ADSERR_NOERR != status) {
		return status;
	}

	// Trigger RECONFIG
	try {
		device.SetState(ADSSTATE_RECONFIG, oldState.device());
	} catch (const AdsException &ex) {
		// ignore AdsError 1861 after RUN/CONFIG mode change
		if (ex.errorCode != 1861) {
			throw;
		}
	}

	// Wait until the reconfiguration has been performed. This is the
	// case once the device actually restarted (the restartCounter
	// changed) and reached config mode. While we are not there yet, we
	// keep polling until the deadline expires.
	const auto deadline = std::chrono::steady_clock::now() +
			      std::chrono::seconds(timeout);
	bool counterChanged = false;

	while (deadline > std::chrono::steady_clock::now()) {
		AdsSysServState newState{};

		if (ADSERR_NOERR == ReadSysServState(device, newState)) {
			if (!counterChanged && (newState.restartCounter() !=
						oldState.restartCounter())) {
				counterChanged = true;
			}

			if (counterChanged &&
			    (ADSSTATE_CONFIG == newState.ads())) {
				return 0;
			}
		}
		std::this_thread::sleep_for(std::chrono::seconds(1));
	}
	return ADSERR_DEVICE_TIMEOUT;
}
}
}
