// SPDX-License-Identifier: MIT
/**
   Copyright (C) Beckhoff Automation GmbH & Co. KG
 */

#pragma once

#include "AdsDevice.h"

namespace bhf
{
namespace ads
{

struct SysServAccess {
	SysServAccess(const std::string &gw, AmsNetId netid, uint16_t port);
	long Reconfig(uint32_t timeout) const;

    private:
	AdsDevice device;
};
}
}
