#!/usr/bin/env python

from gsw import SP_from_C, SA_from_SP, CT_from_t, rho, z_from_p
from gsw.density import sound_speed


def create_practical_salinity_sensor(reader):

    return


def create_density_sensor(reader):

    return


def calculate_practical_salinity(conductivity, temperature, pressure):
    """Calculates practical salinity given glider conductivity, temperature,
    and pressure using Gibbs gsw SP_from_C function.

    Parameters:
        timestamp, conductivity (S/m), temperature (C), and pressure (bar).

    Returns:
        salinity (psu PSS-78).
    """

    # Convert S/m to mS/cm
    ms_conductivity = conductivity * 10

    return SP_from_C(
        ms_conductivity,
        temperature,
        pressure
    )


def calculate_density(temperature, pressure, salinity, latitude, longitude):
    """Calculates density given glider practical salinity, pressure, latitude,
    and longitude using Gibbs gsw SA_from_SP and rho functions.

    Parameters:
        timestamps (UNIX epoch),
        temperature (C), pressure (dbar), salinity (psu PSS-78),
        latitude (decimal degrees), longitude (decimal degrees)

    Returns:
        density (kg/m**3),
    """

    # dBar_pressure = pressure * 10

    absolute_salinity = SA_from_SP(
        salinity,
        pressure,
        longitude,
        latitude
    )

    conservative_temperature = CT_from_t(
        absolute_salinity,
        temperature,
        pressure
    )

    density = rho(
        absolute_salinity,
        conservative_temperature,
        pressure
    )

    return density


def calculate_sound_speed(temperature, pressure, salinity, latitude, longitude):
    """Calculates sound speed given glider practical in-situ temperature, pressure and salinity using Gibbs gsw
    SA_from_SP and rho functions.

    Parameters:
        temperature (C), pressure (dbar), salinity (psu PSS-78), latitude, longitude

    Returns:
        sound speed (m s-1)
    """

    absolute_salinity = SA_from_SP(
        salinity,
        pressure,
        longitude,
        latitude
    )

    conservative_temperature = CT_from_t(
        absolute_salinity,
        temperature,
        pressure
    )

    speed = sound_speed(
        absolute_salinity,
        conservative_temperature,
        pressure
    )

    return speed


def calculate_depth(pressure, latitude):
    """Calculates depth from pressure (dbar) and latitude.  By default, gsw returns depths as negative.  This routine
    returns the absolute values for positive depths.

    Paramters:
        pressure (decibars)
        latitude (decimal degrees)

    Returns:
        depth (meters)
    """

    return abs(z_from_p(pressure, latitude))


def derive_ctd_parameters(dba):
    """
    Derive salinity, density, potential temperature, sound speed,
    and depth from the available CTD sensors in the dba object.
    """

    sensors = dba["sensors"]

    # Required input variables
    cond = sensors.get("sci_water_cond")
    temp = sensors.get("sci_water_temp")
    pres = sensors.get("sci_water_pressure")
    lat = sensors.get("llat_latitude")
    lon = sensors.get("llat_longitude")

    if None in (cond, temp, pres, lat, lon):
        raise ValueError("Missing one or more CTD source variables.")

    # Derived variables
    sal = calculate_practical_salinity(cond, temp, pres)
    sensors["salinity"] = sal

    sensors["density"] = calculate_density(temp, pres, sal, lat, lon)
    sensors["potential_temperature"] = CT_from_t(SA_from_SP(sal, pres, lon, lat), temp, pres)
    sensors["sound_speed"] = calculate_sound_speed(temp, pres, sal, lat, lon)
    sensors["depth"] = calculate_depth(pres, lat)

    return dba
