#include <array>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <span>

#include "stockbuild/kbar_schema.hpp"
#include "stockbuild/strategy_runtime.hpp"

int main() {
    using stockbuild::KBarRecord;
    static_assert(sizeof(KBarRecord) == 56);
    static_assert(alignof(KBarRecord) == 8);
    static_assert(offsetof(KBarRecord, close) == 32);
    static_assert(offsetof(KBarRecord, flags) == 48);

    const std::array<std::int64_t, 6> timestamps{0, 1, 2, 3, 4, 5};
    const std::array<std::int64_t, 6> days{0, 0, 0, 0, 0, 0};
    const std::array<double, 6> close{100, 101, 104, 103, 95, 94};
    const std::array<double, 6> execution{101, 102, 110, 109, 90, 94};
    const std::array<std::uint8_t, 6> entry{0, 0, 1, 0, 0, 0};
    const std::span<const std::uint8_t> entry_span{entry};
    const std::array<std::span<const std::uint8_t>, 1> entries{entry_span};
    stockbuild::StrategyRuntimeConfig config;
    config.stop_loss_abs = 10.0;
    const auto intents = stockbuild::evaluate_strategy_runtime(
        timestamps, days, close, execution, entries, {}, config, 2, 5);
    assert(intents.kind[2] == 1 && intents.price[2] == 110.0);
    assert(intents.kind[4] == 2 && intents.reason[4] == 5 && intents.price[4] == 90.0);
    assert(intents.kind[5] == 0);

    std::cout << "abi=" << stockbuild::kAbiVersion
              << " schema=" << stockbuild::kKBarSchemaVersion
              << " size=" << sizeof(KBarRecord) << '\n';
    return 0;
}
