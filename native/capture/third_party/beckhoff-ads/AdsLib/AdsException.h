// SPDX-License-Identifier: MIT
/**
   Copyright (c) Beckhoff Automation GmbH & Co. KG
 */

#pragma once

#include <cstdio>
#include <exception>

struct AdsException : std::exception {
	AdsException(const long adsErrorCode) noexcept : errorCode(adsErrorCode)
	{
		static constexpr char msg[] =
			"Ads operation failed with error code ";
		static_assert(sizeof(m_Message) >= sizeof(msg) + 20 + 1,
			      "Message too long");
		std::snprintf(m_Message, sizeof(m_Message), "%s%ld.", msg,
			      adsErrorCode);
	}

	const char *what() const throw() override
	{
		return m_Message;
	}

	const long errorCode;

    private:
	/** 37 character is our message prefix
	    20 LONG_MIN with sign
	     2 "." at the end of the message and terminating NUL
	     5 bytes left for padding
	*/
	char m_Message[64];
};
