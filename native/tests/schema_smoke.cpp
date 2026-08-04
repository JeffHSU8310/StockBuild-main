#include <cstddef>
#include <iostream>

#include "stockbuild/kbar_schema.hpp"

int main() {
    using stockbuild::KBarRecord;
    if (sizeof(KBarRecord) != 56 || alignof(KBarRecord) != 8 ||
        offsetof(KBarRecord, close) != 32 || offsetof(KBarRecord, flags) != 48) {
        return 1;
    }
    std::cout << "abi=" << stockbuild::kAbiVersion
              << " schema=" << stockbuild::kKBarSchemaVersion
              << " size=" << sizeof(KBarRecord) << '\n';
    return 0;
}
