#pragma once
// Optional observation channel. Never writes files, blocks, retries, or changes
// a robot command. Unix datagrams stay on PC2; absent/full receiver drops data.
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <string>

namespace g1d {
class RecordingSink {
    int fd_ = -1;
    sockaddr_un address_{};
    unsigned long sequence_ = 0, dropped_ = 0;
public:
    RecordingSink() noexcept {
        const char* path = std::getenv("G1D_RECORD_SOCKET");
        if (!path || std::strlen(path) >= sizeof(address_.sun_path)) return;
        address_.sun_family = AF_UNIX;
        std::strcpy(address_.sun_path, path);
        fd_ = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    }
    ~RecordingSink() { if (fd_ >= 0) close(fd_); }
    RecordingSink(const RecordingSink&) = delete;
    RecordingSink& operator=(const RecordingSink&) = delete;
    bool enabled() const { return fd_ >= 0; }
    void Send(const char* stream, const std::string& payload, double now) noexcept {
        if (!enabled()) return;
        try {
            std::ostringstream out;
            out << std::setprecision(15) << "{\"stream\":\"" << stream
                << "\",\"producer\":\"cpp:" << getpid() << "\",\"seq\":" << ++sequence_
                << ",\"emitted_monotonic_s\":" << now << ",\"dropped_total\":" << dropped_
                << ",\"data\":" << payload << '}';
            const auto text = out.str();
            if (text.size() > 60000 || sendto(fd_, text.data(), text.size(), MSG_DONTWAIT | MSG_NOSIGNAL,
                    reinterpret_cast<sockaddr*>(&address_), sizeof(address_)) != static_cast<ssize_t>(text.size()))
                ++dropped_;
        } catch (...) { ++dropped_; }
    }
};
template <class Values> inline void JsonArray(std::ostream& out, const Values& values) {
    out << '[';
    bool first = true;
    for (auto value : values) {
        if (!first) out << ',';
        out << static_cast<float>(value);
        first = false;
    }
    out << ']';
}
} // namespace g1d
