// +------------------------------------------------------------------+
// |             ____ _               _        __  __ _  __           |
// |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
// |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
// |           | |___| | | |  __/ (__|   <    | |  | | . \            |
// |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
// |                                                                  |
// | Copyright Mathias Kettner 2015             mk@mathias-kettner.de |
// +------------------------------------------------------------------+
//
// This file is part of Check_MK.
// The official homepage is at http://mathias-kettner.de/check_mk.
//
// check_mk is free software;  you can redistribute it and/or modify it
// under the  terms of the  GNU General Public License  as published by
// the Free Software Foundation in version 2.  check_mk is  distributed
// in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
// out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
// PARTICULAR PURPOSE. See the  GNU General Public License for more de-
// ails.  You should have  received  a copy of the  GNU  General Public
// License along with GNU Make; see the file  COPYING.  If  not,  write
// to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
// Boston, MA 02110-1301 USA.


#include "OutputProxy.h"
#include "logging.h"
#include <cstdarg>
#include <winsock2.h>


// urgh
extern volatile bool g_should_terminate;


FileOutputProxy::FileOutputProxy(FILE *file)
    : _file(file)
{}

void FileOutputProxy::output(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    vfprintf(_file, format, ap);
    va_end(ap);
}

void FileOutputProxy::writeBinary(const char *buffer, size_t size)
{
    fwrite(buffer, 1, size, _file);
}

void FileOutputProxy::flush() {
    // nop
}


BufferedSocketProxy::BufferedSocketProxy(SOCKET socket, size_t buffer_size)
    : _socket(socket)
{
    _buffer.resize(buffer_size);
}


void BufferedSocketProxy::setSocket(SOCKET socket)
{
    _socket = socket;
}


void BufferedSocketProxy::output(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);

    int written_len = vsnprintf(&_buffer[0] + _length, _buffer.size() - _length, format, ap);
    va_end(ap);
    _length += written_len;

    // We do not send out the data immediately
    // This would lead to many small tcp packages
    if (_length > MIN_OUTPUT_SIZE) {
        flushInt();
    }
}


void BufferedSocketProxy::writeBinary(const char *buffer, size_t size)
{
    if (_buffer.size() - _length >= size) {
        memcpy(&_buffer[0] + _length, buffer, size);
        _length += size;

        if (_length > MIN_OUTPUT_SIZE) {
            flushInt();
        }
    }
}


void BufferedSocketProxy::flush()
{
    int tries = 10;
    while ((_length > 0) && (tries > 0)) {
        --tries;
        flushInt();
        if (_length > 0) {
            ::Sleep(100);
        }
    }
    if (_length > 0) {
        verbose("failed to flush entire buffer\n");
    }
}


void BufferedSocketProxy::flushInt()
{
    size_t offset = 0;
    while (!g_should_terminate) {
        ssize_t result = send(_socket, &_buffer[0] + offset, _length - offset, 0);
        if (result == SOCKET_ERROR) {
            int error = WSAGetLastError();
            if (error == WSAEINTR) {
                continue;
            }
            else if (error == WSAEINPROGRESS) {
                continue;
            }
            else if (error == WSAEWOULDBLOCK) {
                verbose("send to socket would block");
                break;
            }
            else {
                verbose("send to socket failed with error code %d",
                        error);
                break;
            }
        }
        else if (result == 0) {
            // nothing written, which means the socket-cache is
            // probably full 
        }
        else {
            offset += result;
        }

        break;
    }
    _length -= offset;
    if (_length != 0) {
        // not the whole buffer has been sent, shift up the remaining data
        memmove(&_buffer[0], &_buffer[0] + offset, _length);
    }
}


EncryptingBufferedSocketProxy::EncryptingBufferedSocketProxy(SOCKET socket,
        const std::string &passphrase, size_t buffer_size)
    : BufferedSocketProxy(socket, buffer_size)
    , _crypto(passphrase)
{
    _blockSize = _crypto.blockSize();

    _plain.resize(_blockSize * 8);
}


void EncryptingBufferedSocketProxy::output(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);

    int buffer_left = _plain.size() - _written;
    int written_len = vsnprintf(&_plain[0] + _written, buffer_left, format, ap);
    if (written_len > buffer_left) {
        _plain.resize(_written + written_len + _blockSize);
        buffer_left = _plain.size() - _written;
        written_len = vsnprintf(&_plain[0] + _written, buffer_left, format, ap);
    }
    va_end(ap);
    _written += written_len;

    if (_written >= _blockSize) {
        // we have at least one block of data. encrypt it and push it to the
        // underlying send buffer
        size_t push_size = (_written / _blockSize) * _blockSize;
        std::vector<char> push_buf(_plain);

        DWORD required_size = _crypto.encrypt(NULL, push_size, push_buf.size(), false);
        if (required_size > push_buf.size()) {
            push_buf.resize(required_size);
        }
        _crypto.encrypt(reinterpret_cast<BYTE*>(&push_buf[0]), push_size, push_buf.size(), false);
        writeBinary(&push_buf[0], required_size);

        memmove(&_plain[0], &_plain[push_size], _written - push_size);
        _written -= push_size;
    }
}


void EncryptingBufferedSocketProxy::flush()
{
    // this assumes the plain buffer is large enouph for one measly block
    DWORD required_size = _crypto.encrypt(reinterpret_cast<BYTE*>(&(_plain)[0]), _written,
                                          _plain.size(), true);
    writeBinary(&_plain[0], required_size);

    _written = 0;

    BufferedSocketProxy::flush();
}

