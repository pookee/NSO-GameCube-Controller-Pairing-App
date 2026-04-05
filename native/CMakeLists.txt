cmake_minimum_required(VERSION 3.10)
project(gc-enabler C)

find_package(PkgConfig)
if(PkgConfig_FOUND)
    pkg_check_modules(LIBUSB REQUIRED libusb-1.0)
else()
    # Fallback: assume libusb-1.0 headers/libs are on the default search path
    set(LIBUSB_INCLUDE_DIRS "")
    set(LIBUSB_LIBRARIES usb-1.0)
endif()

add_executable(gc-enabler gc-enabler.c)
target_include_directories(gc-enabler PRIVATE ${LIBUSB_INCLUDE_DIRS})
target_link_libraries(gc-enabler ${LIBUSB_LIBRARIES})

install(TARGETS gc-enabler DESTINATION bin)
