#ifndef PYC_EXPORT_H
#define PYC_EXPORT_H

#include "pyc_module.h"
#include "pyc_numeric.h"
#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>

// Export the decrypted object tree for independent, static decompiler backends.
// This does not execute code or remove runtime marker calls.
class PycMarshalWriter {
public:
    explicit PycMarshalWriter(std::ostream& stream) : out(stream) {}

    void integer(uint32_t value, int bytes = 4) {
        for (int i = 0; i < bytes; ++i)
            out.put(static_cast<char>(value >> (i * 8)));
    }

    void object(PycRef<PycObject> obj) {
        if (!obj) {
            out.put('N');
            return;
        }
        auto code = obj.try_cast<PycCode>();
        if (code) {
            out.put('c');
            integer(code->argCount());
            integer(code->posOnlyArgCount());
            integer(code->kwOnlyArgCount());
            integer(code->stackSize());
            integer(code->flags() & ~PycCode::CO_PYARMOR_OBFUSCATED);
            object(code->code().cast<PycObject>());
            object(code->consts().cast<PycObject>());
            object(code->names().cast<PycObject>());
            object(code->localNames().cast<PycObject>());
            object(code->localKinds().cast<PycObject>());
            object(code->fileName().cast<PycObject>());
            object(code->name().cast<PycObject>());
            object(code->qualName().cast<PycObject>());
            integer(code->firstLine());
            object(code->lnTable().cast<PycObject>());
            object(code->exceptTable().cast<PycObject>());
            return;
        }
        auto string = obj.try_cast<PycString>();
        if (string) {
            int type = obj.type();
            bool bytes = type == PycObject::TYPE_STRING;
            out.put(bytes ? 's' : 'u');
            integer(string->length());
            out.write(string->value(), string->length());
            return;
        }
        auto sequence = obj.try_cast<PycSimpleSequence>();
        if (sequence) {
            int type = obj.type();
            out.put(type == PycObject::TYPE_SMALL_TUPLE ? '(' : type);
            if (type != PycObject::TYPE_SLICE)
                integer(sequence->values().size());
            for (const auto& value : sequence->values())
                object(value);
            return;
        }
        auto dict = obj.try_cast<PycDict>();
        if (dict) {
            out.put('{');
            for (const auto& pair : dict->values()) {
                object(std::get<0>(pair));
                object(std::get<1>(pair));
            }
            out.put('0');
            return;
        }
        auto small = obj.try_cast<PycInt>();
        if (small) {
            out.put('i');
            integer(small->value());
            return;
        }
        auto large = obj.try_cast<PycLong>();
        if (large) {
            out.put('l');
            integer(large->size());
            for (int value : large->value())
                integer(value, 2);
            return;
        }
        auto complex = obj.try_cast<PycCComplex>();
        if (complex) {
            out.put('y');
            floating(complex->value());
            floating(complex->imag());
            return;
        }
        auto real = obj.try_cast<PycCFloat>();
        if (real) {
            out.put('g');
            floating(real->value());
            return;
        }
        switch (obj.type()) {
        case PycObject::TYPE_NONE:
        case PycObject::TYPE_FALSE:
        case PycObject::TYPE_TRUE:
        case PycObject::TYPE_STOPITER:
        case PycObject::TYPE_ELLIPSIS:
            out.put(obj.type());
            return;
        default:
            throw std::runtime_error("Unsupported constant type for modern .pyc export");
        }
    }

private:
    std::ostream& out;
    void floating(double value) {
        uint64_t bits;
        std::memcpy(&bits, &value, sizeof(bits));
        integer(static_cast<uint32_t>(bits));
        integer(static_cast<uint32_t>(bits >> 32));
    }
};

inline void export_pyc(PycModule& mod, const std::string& path) {
    if (mod.majorVer() != 3 || mod.minorVer() < 11 || mod.minorVer() > 14)
        throw std::runtime_error(".pyc export requires CPython 3.11 through 3.14");
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out)
        throw std::runtime_error("Cannot open .pyc export output");
    const unsigned int magic[] = {MAGIC_3_11, MAGIC_3_12, MAGIC_3_13, MAGIC_3_14};
    PycMarshalWriter writer(out);
    writer.integer(magic[mod.minorVer() - 11]);
    writer.integer(0);
    writer.integer(0);
    writer.integer(0);
    writer.object(mod.code().cast<PycObject>());
    if (!out)
        throw std::runtime_error("Cannot write .pyc export output");
}

#endif
