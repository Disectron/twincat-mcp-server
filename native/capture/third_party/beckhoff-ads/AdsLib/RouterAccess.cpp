// SPDX-License-Identifier: MIT
/**
   Copyright (c) Beckhoff Automation GmbH & Co. KG
 */

#include "RouterAccess.h"
#include "Log.h"
#include "wrap_endian.h"
#include <array>
#include <iomanip>
#include <iostream>

namespace bhf
{
namespace ads
{
struct SearchPciBusReq {
	SearchPciBusReq(const uint64_t quad)
		: leVendorID(bhf::ads::htole<uint16_t>(
			  static_cast<uint16_t>(quad >> 48)))
		, leDeviceID(bhf::ads::htole<uint16_t>(
			  static_cast<uint16_t>(quad >> 32)))
		, leSubVendorID(bhf::ads::htole<uint16_t>(
			  static_cast<uint16_t>(quad >> 16)))
		, leSubSystemID(bhf::ads::htole<uint16_t>(
			  static_cast<uint16_t>(quad)))
	{
	}

    private:
	const uint16_t leVendorID;
	const uint16_t leDeviceID;
	const uint16_t leSubVendorID;
	const uint16_t leSubSystemID;
};

struct SearchPciSlotResNew {
	static constexpr size_t MAXBASEADDRESSES = 6;
	std::array<uint32_t, MAXBASEADDRESSES> leBaseAddresses;
	uint32_t leSize[MAXBASEADDRESSES];
	uint32_t leBusNumber;
	uint32_t leSlotNumber;
	uint16_t leBoardIrq;
	uint16_t lePciRegViaPorts;
};

struct SearchPciBusResNew {
	static constexpr size_t MAXSLOTRESPONSE = 64;
	uint32_t leFound;
	std::array<SearchPciSlotResNew, MAXSLOTRESPONSE> slot;
	uint32_t nFound() const
	{
		return bhf::ads::letoh(leFound);
	}
};

std::ostream &operator<<(std::ostream &os, const SearchPciSlotResNew &slot)
{
	const auto high = uint64_t(bhf::ads::letoh(slot.leBaseAddresses[1]));
	const auto low = uint64_t(bhf::ads::letoh(slot.leBaseAddresses[0]));
	const auto address = high << 32 | low;
	return os << std::dec << bhf::ads::letoh(slot.leBusNumber) << ':'
		  << bhf::ads::letoh(slot.leSlotNumber) << " @ 0x" << std::hex
		  << std::setw(16) << std::setfill('0') << address;
}

RouterAccess::RouterAccess(const std::string &gw, const AmsNetId netid,
			   const uint16_t port)
	: device(gw, netid, port ? port : 1)
{
}

bool RouterAccess::PciScan(const uint64_t pci_id, std::ostream &os) const
{
#define ROUTERADSGRP_ACCESS_HARDWARE 0x00000005
#define ROUTERADSOFFS_A_HW_SEARCHPCIBUS 0x00000003

	SearchPciBusResNew res{};
	uint32_t bytesRead = 0;

	const auto req = SearchPciBusReq{ pci_id };
	const auto status = device.ReadWriteReqEx2(
		ROUTERADSGRP_ACCESS_HARDWARE, ROUTERADSOFFS_A_HW_SEARCHPCIBUS,
		sizeof(res), &res, sizeof(req), &req, &bytesRead);

	if (ADSERR_NOERR != status) {
		LOG_ERROR(__FUNCTION__ << "(): failed with: 0x" << std::hex
				       << status << '\n');
		return false;
	}

	if (bytesRead < sizeof(res.leFound)) {
		LOG_ERROR(__FUNCTION__ << "(): response too short to hold a "
					  "slot count: "
				       << std::dec << bytesRead << '\n');
		return false;
	}

	if (res.slot.size() < res.nFound()) {
		LOG_WARN(__FUNCTION__ << "(): data seems corrupt. Slot count 0x"
				      << std::hex << res.nFound()
				      << " exceeds maximum 0x"
				      << res.slot.size() << " -> truncating\n");
	}

	/** res is zero initialised, so slots the device did not send would
	 * print as empty entries instead of being left out.
	 */
	const auto received =
		(bytesRead - sizeof(res.leFound)) / sizeof(res.slot[0]);
	auto limit = std::min<size_t>(res.slot.size(), res.nFound());
	limit = std::min<size_t>(limit, received);
	os << "PCI devices found: " << std::dec << limit << '\n';
	for (const auto &slot : res.slot) {
		if (!limit--) {
			break;
		}
		os << slot << '\n';
	}
	return !os.good();
}
}
}
