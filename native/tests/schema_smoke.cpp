#include <cstddef>
#include <iostream>

#include "stockbuild/kbar_schema.hpp"

int main() {
    using stockbuild::KBarRecord;
    static_assert(sizeof(KBarRecord) == 56);
    static_assert(alignof(KBarRecord) == 8);
    static_assert(offsetof(KBarRecord, close) == 32);
    static_assert(offsetof(KBarRecord, flags) == 48);
    std::cout << "abi=" << stockbuild::kAbiVersion
              << " schema=" << stockbuild::kKBarSchemaVersion
              << " size=" << sizeof(KBarRecord) << '\n';
    return 0;
}
