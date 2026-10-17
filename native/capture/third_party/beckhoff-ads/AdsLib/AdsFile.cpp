// SPDX-License-Identifier: MIT
/**
   Copyright (c) 2020 - 2022 Beckhoff Automation GmbH & Co. KG
 */

#include "AdsFile.h"
#include "Log.h"
#include <iostream>
#include <list>
#include <vector>

#pragma pack(push, 1)
/**
 * @brief This structure describes file information reveived via ADS
 *
 * Calling ReadWriteReqEx2 with IndexGroup == SYSTEMSERVICE_FFILEFIND
 * will return ADS file information in the provided readData buffer.
 * The data of that information is structured as TcFileFindData.
 */
struct TcFileFindData {
	uint32_t hFile;
	uint32_t dwFileAttributes;
	uint64_t nReserved1[5];
	char cFileName[260];
	char unused[14];

	bool isDirectory(void) const
	{
		return 0x10 & dwFileAttributes;
	}

	void letoh(void)
	{
		hFile = bhf::ads::letoh(hFile);
		dwFileAttributes = bhf::ads::letoh(dwFileAttributes);
	}
};
#pragma pack(pop)

static bool FindNext(const AdsDevice &route, TcFileFindData &child,
		     const size_t length = 0, const char *const path = nullptr)
{
	uint32_t bytesRead = 0;
	const auto error = route.ReadWriteReqEx2(SYSTEMSERVICE_FFILEFIND,
						 child.hFile, sizeof(child),
						 &child, length, path,
						 &bytesRead);
	// We reached the last child
	// If there is no more file in the current path the ADS service will
	// return ads error code 1804 so we can break and exit as expected.
	if (error == 1804) {
		return true;
	}
	if (error) {
		throw AdsException(error);
	}
	if (bytesRead != sizeof(child)) {
		LOG_ERROR(__FUNCTION__ << "(): read " << std::dec << bytesRead
				       << " bytes, expected " << sizeof(child)
				       << '\n');
		throw AdsException(ADSERR_DEVICE_INVALIDDATA);
	}
	// The device fills cFileName up to its last byte, but we hand it out
	// as a NUL terminated string, so terminate it ourselves.
	child.cFileName[sizeof(child.cFileName) - 1] = '\0';
	// TwinCAT sends data in little endian so we have to convert it here
	child.letoh();
	return false;
}

static bool FindFirst(const AdsDevice &route, TcFileFindData &item,
		      const std::string &path)
{
	enum FFILEFIND : uint32_t {
		GENERIC = 1 << 0,
	};

	item.hFile = FFILEFIND::GENERIC;
	return FindNext(route, item, path.length(), path.c_str());
}

AdsFile::AdsFile(const AdsDevice &route, const std::string &filename,
		 const uint32_t flags)
	: m_Route(route)
	, m_Handle(route.OpenFile(filename, flags))
{
}

void AdsFile::Delete(const AdsDevice &route, const std::string &filename,
		     const uint32_t flags)
{
	auto error = route.ReadWriteReqEx2(SYSTEMSERVICE_FDELETE, flags, 0,
					   nullptr, filename.length(),
					   filename.c_str(), nullptr);

	if (error) {
		throw AdsException(error);
	}
}

int AdsFile::Find(const AdsDevice &route, const std::string &basePath,
		  const size_t maxdepth, std::ostream &os)
{
	struct Path {
		size_t depth;
		std::string path;
	};
	std::list<struct Path> pendingDirs{ { 0, basePath } };
	while (!pendingDirs.empty()) {
		auto path = pendingDirs.front().path;
		auto depth = pendingDirs.front().depth;
		pendingDirs.pop_front();

		TcFileFindData parent;
		if (FindFirst(route, parent, path)) {
			return 1804;
		}

		// Path exists print it and prepare traversing
		os << path << '\n';

		if (parent.isDirectory() && (depth < maxdepth)) {
			// Finding files in a directory is a bit weird. We get only one entry per call and for
			// every call we pass the last found item to get the next. The first item is special.
			// To get the children of a directory we have to append '/*'to the path of the directory.
			for (auto last = FindFirst(route, parent, path + "/*");
			     !last; last = FindNext(route, parent)) {
				if (parent.isDirectory()) {
					pendingDirs.push_back(
						{ depth + 1,
						  path + '/' +
							  parent.cFileName });
				} else {
					os << path << '/' << parent.cFileName
					   << '\n';
				}
			}
		}
	}
	return 0;
}

void AdsFile::Read(const size_t size, void *data, uint32_t &bytesRead) const
{
	auto error = m_Route.ReadWriteReqEx2(SYSTEMSERVICE_FREAD, *m_Handle,
					     size, data, 0, nullptr,
					     &bytesRead);

	if (error) {
		throw AdsException(error);
	}
}

void AdsFile::Rename(const AdsDevice &route, const std::string &source,
		     const std::string &destination, const uint32_t flags)
{
	// The write payload is the source and destination path, each
	// terminated by a null byte: <source>'\0'<destination>'\0'
	std::vector<char> buffer;
	buffer.reserve(source.length() + destination.length() + 2);
	buffer.insert(buffer.end(), source.begin(), source.end());
	buffer.push_back('\0');
	buffer.insert(buffer.end(), destination.begin(), destination.end());
	buffer.push_back('\0');

	auto error = route.ReadWriteReqEx2(SYSTEMSERVICE_FRENAME, flags, 0,
					   nullptr, buffer.size(),
					   buffer.data(), nullptr);

	if (error) {
		throw AdsException(error);
	}
}

void AdsFile::Write(const size_t size, const void *data) const
{
	auto error = m_Route.ReadWriteReqEx2(SYSTEMSERVICE_FWRITE, *m_Handle, 0,
					     nullptr, size, data, nullptr);
	if (error) {
		throw AdsException(error);
	}
}
