"""STAR SHADOW
Satellite Time-series Analysis Routine using
Sinusoids and Harmonics in an Automated way for Double stars with Occultations and Waves

This Python module contains the main functions that link together all functionality.

Code written by: Luc IJspeert
"""

import os
import time
import numpy as np
import scipy as sp
import scipy.signal
import functools as fct
import multiprocessing as mp

from . import timeseries_functions as tsf
from . import timeseries_fitting as tsfit
from . import analysis_functions as af
from . import utility as ut


def frequency_analysis_porb(times, signal, f_n, a_n, ph_n, noise_level):
    """Find the most likely eclipse period from a sinusoid model
    
    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    
    Returns
    -------
    p_orb: float
        Orbital period of the eclipsing binary in days
    
    Notes
    -----
    Uses a combination of phase dispersion minimisation and
    Lomb-Scargle periodogram (see Saha & Vivas 2017), and some
    refining steps to get the best period.
    """
    t_tot = np.ptp(times)
    freq_res = 1.5 / t_tot  # Rayleigh criterion
    f_nyquist = 1 / (2 * np.min(np.diff(times)))
    # first to get a global minimum do combined PDM and LS, at select frequencies
    periods, phase_disp = tsf.phase_dispersion_minimisation(times, signal, f_n, local=False)
    ampls = tsf.scargle_ampl(times, signal - np.mean(signal), 1/periods)
    psi_measure = ampls / phase_disp
    # also check the number of harmonics at each period and include into best f
    n_harm, completeness, distance = af.harmonic_series_length(1/periods, f_n, freq_res, f_nyquist)
    psi_h_measure = psi_measure * n_harm * completeness
    # select the best period, refine it and check double P
    base_p = periods[np.argmax(psi_h_measure)]
    # refine by using a dense sampling
    f_refine = np.arange(0.99 / base_p, 1.01 / base_p, 0.0001 / base_p)
    n_harm_r, completeness_r, distance_r = af.harmonic_series_length(f_refine, f_n, freq_res, f_nyquist)
    h_measure = n_harm_r * completeness_r
    mask_peak = (n_harm_r == np.max(n_harm_r))
    i_min_dist = np.argmin(distance_r[mask_peak])
    p_orb = 1 / f_refine[mask_peak][i_min_dist]
    # check twice the period as well
    base_p2 = base_p * 2
    # refine by using a dense sampling
    f_refine_2 = np.arange(0.99 / base_p2, 1.01 / base_p2, 0.0001 / base_p2)
    n_harm_r_2, completeness_r_2, distance_r_2 = af.harmonic_series_length(f_refine_2, f_n, freq_res, f_nyquist)
    h_measure_2 = n_harm_r_2 * completeness_r_2
    mask_peak_2 = (n_harm_r_2 == np.max(n_harm_r_2))
    i_min_dist_2 = np.argmin(distance_r_2[mask_peak_2])
    p_orb_2 = 1 / f_refine_2[mask_peak_2][i_min_dist_2]
    # compare the length and completeness to decide, using a threshold
    minimal_frac = 1.5  # empirically determined threshold
    frac_double = h_measure_2[mask_peak_2][i_min_dist_2] / h_measure[mask_peak][i_min_dist]
    if (frac_double > minimal_frac):
        p_orb = p_orb_2
    return p_orb


def frequency_analysis(tic, times, signal, signal_err, i_sectors, p_orb, save_dir, data_id=None, overwrite=False,
                       verbose=False):
    """Recipe for analysis of EB light curves.

    Parameters
    ----------
    tic: int
        The TESS Input Catalog number for later reference
        Use any number (or even str) as reference if not available.
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    p_orb: float
        The orbital period. Set 0 to search for the best period.
        If the orbital period is known with certainty beforehand, it can
        be provided as initial value and no new period will be searched.
    save_dir: str
        Path to a directory for saving the results. Also used to load
        previous analysis results.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    p_orb_i: list[float]
        Orbital period at each stage of the analysis
    const_i: list[numpy.ndarray[float]]
        Y-intercept(s) of a piece-wise linear curve for each stage of the analysis
    slope_i: list[numpy.ndarray[float]]
        Slope(s) of a piece-wise linear curve for each stage of the analysis
    f_n_i: list[numpy.ndarray[float]]
        Frequencies of a number of sine waves for each stage of the analysis
    a_n_i: list[numpy.ndarray[float]]
        Amplitudes of a number of sine waves for each stage of the analysis
    ph_n_i: list[numpy.ndarray[float]]
        Phases of a number of sine waves for each stage of the analysis

    Notes
    -----
    1) Extract frequencies
        We start by extracting the frequency of highest amplitude one by one, directly from
        the Lomb-Scargle periodogram until the BIC does not significantly improve anymore.
        No fitting is performed yet.
    2) First multi-sine NL-LS fit
        To get the best results in the following steps, a fit is performed over sets of 10-15
        frequencies at a time. Fitting in groups is a trade-off between accuracy and
        drastically reduced time taken.
    3) Measure the orbital period and couple the harmonic frequencies
        Find the orbital period from the longest series of harmonics.
        Find the harmonics with the orbital period, measure a better period
        from the harmonics and set the frequencies of the harmonics to their new values.
        [Note: it is possible to provide a fixed period if it is already well known. It will
        still be included as a free parameter in the fits]
    4) Attempt to extract a few more orbital harmonics
        With the decreased number of free parameters (2 vs. 3), the BIC, which punishes
        for free parameters, may allow the extraction of a few more harmonics.
    5) Additional non-harmonics may be found
        Step 3 involves removing frequencies close to the harmonics. These may have included
        actual non-harmonic frequencies. It is attempted to extract these again here.
    6) Multi-NL-LS fit with coupled harmonics
        Fit once again in (larger) groups of frequencies, including the orbital period and the coupled
        harmonics.
    7) Attempt to remove frequencies
        After fitting, it is possible that certain frequencies are better removed than kept.
        This also looks at replacing groups of close frequencies by a single frequency.
        All harmonics are kept at the same frequency.
    8) (only if frequencies removed) Multi-NL-LS fit with coupled harmonics
        If the previous step removed some frequencies, we need to fit one final time.
    """
    t_0a = time.time()
    n_sectors = len(i_sectors)
    freq_res = 1.5 / np.ptp(times)  # Rayleigh criterion
    signal_err = np.max(signal_err) * np.ones(len(times))  # likelihood assumes the same errors
    # for saving, make a folder if not there yet
    if not os.path.isdir(os.path.join(save_dir, f'tic_{tic}_analysis')):
        os.mkdir(os.path.join(save_dir, f'tic_{tic}_analysis'))  # create the subdir
    # ---------------------------------------------------
    # [1] --- initial iterative extraction of frequencies
    # ---------------------------------------------------
    file_name_1 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_1.hdf5')
    if os.path.isfile(file_name_1) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_1, verbose=verbose)
        p_orb_1, const_1, slope_1, f_n_1, a_n_1, ph_n_1 = results
        model_1 = tsf.linear_curve(times, const_1, slope_1, i_sectors)
        model_1 += tsf.sum_sines(times, f_n_1, a_n_1, ph_n_1)
        if verbose:
            print(f'Step 1: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 1: Starting initial frequency extraction')
        t1_a = time.time()
        const_1, slope_1, f_n_1, a_n_1, ph_n_1 = tsf.extract_all(times, signal, signal_err, i_sectors, verbose=verbose)
        t1_b = time.time()
        # main function done, do the rest for this step
        model_1 = tsf.linear_curve(times, const_1, slope_1, i_sectors)
        model_1 += tsf.sum_sines(times, f_n_1, a_n_1, ph_n_1)
        n_param_1 = 2 * n_sectors + 3 * len(f_n_1)
        bic_1 = tsf.calc_bic((signal - model_1)/signal_err, n_param_1)
        # now print some useful info and/or save the result
        if verbose:
            print(f'\033[1;32;48mInitial frequency extraction complete.\033[0m')
            print(f'\033[0;32;48m{len(f_n_1)} frequencies, {n_param_1} free parameters. '
                  f'BIC: {bic_1:1.2f}, time taken: {t1_b - t1_a:1.1f}s\033[0m\n')
        # save
        results = (0, const_1, slope_1, f_n_1, a_n_1, ph_n_1)
        f_errors = tsf.formal_uncertainties(times, signal - model_1, a_n_1, i_sectors)
        c_err_1, sl_err_1, f_n_err_1, a_n_err_1, ph_n_err_1 = f_errors
        errors = (-1, c_err_1, sl_err_1, f_n_err_1, a_n_err_1, ph_n_err_1)
        stats = (n_param_1, bic_1, np.std(signal - model_1))
        desc = '[1] Initial frequency extraction results.'
        ut.save_results(results, errors, stats, file_name_1, description=desc, data_id=data_id)
    # ---------------------------------------------------
    # [2] --- do a first multi-sine NL-LS fit (in chunks)
    # ---------------------------------------------------
    file_name_2 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_2.hdf5')
    if os.path.isfile(file_name_2) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_2, verbose=verbose)
        p_orb_2, const_2, slope_2, f_n_2, a_n_2, ph_n_2 = results
        p_err_2, c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2 = errors
        n_param_2, bic_2, noise_level_2 = stats
        model_2 = tsf.linear_curve(times, const_2, slope_2, i_sectors)
        model_2 += tsf.sum_sines(times, f_n_2, a_n_2, ph_n_2)
        if verbose:
            print(f'Step 2: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 2: Starting multi-sine NL-LS fit.')
        t_2a = time.time()
        f_groups = ut.group_fequencies_for_fit(a_n_1, g_min=10, g_max=15)
        out_2 = tsfit.fit_multi_sinusoid_per_group(times, signal, signal_err, const_1, slope_1, f_n_1, a_n_1, ph_n_1,
                                                   i_sectors, f_groups, verbose=verbose)
        t_2b = time.time()
        # main function done, do the rest for this step
        const_2, slope_2, f_n_2, a_n_2, ph_n_2 = out_2
        model_2 = tsf.linear_curve(times, const_2, slope_2, i_sectors)
        model_2 += tsf.sum_sines(times, f_n_2, a_n_2, ph_n_2)
        noise_level_2 = np.std(signal - model_2)
        f_errors = tsf.formal_uncertainties(times, signal - model_2, a_n_2, i_sectors)
        c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2 = f_errors
        n_param_2 = 2 * n_sectors + 3 * len(f_n_2)
        bic_2 = tsf.calc_bic((signal - model_2)/signal_err, n_param_2)
        # now print some useful info and/or save the result
        if verbose:
            print(f'\033[1;32;48mFit complete.\033[0m')
            print(f'\033[0;32;48m{len(f_n_2)} frequencies, {n_param_2} free parameters. '
                  f'BIC: {bic_2:1.2f}, time taken: {t_2b - t_2a:1.1f}s\033[0m\n')
        # save
        results = (0, const_2, slope_2, f_n_2, a_n_2, ph_n_2)
        errors = (-1, c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2)
        stats = (n_param_2, bic_2, noise_level_2)
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_2.hdf5')
        desc = '[2] First multi-sine NL-LS fit results.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
        # save freqs and linear curve in ascii format at this stage
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_2_sinusoid.csv')
        data = np.column_stack((f_n_2, f_n_err_2, a_n_2, a_n_err_2, ph_n_2, ph_n_err_2))
        hdr = f'f_n_2, f_n_err_2, a_n_2, a_n_err_2, ph_n_2, ph_n_err_2'
        np.savetxt(file_name, data, delimiter=',', header=hdr)
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_2_linear.csv')
        data = np.column_stack((const_2, c_err_2, slope_2, sl_err_2, i_sectors[:, 0], i_sectors[:, 1]))
        hdr = (f'const_2, c_err_2, slope_2, sl_err_2, sector_start, sector_end')
        np.savetxt(file_name, data, delimiter=',', header=hdr)
    if (len(f_n_2) == 0):
        if verbose:
            print(f'No frequencies found.')
        # save and return
        fn_ext = os.path.splitext(os.path.basename(file_name_2))[1]
        np.savetxt(file_name_2.replace(fn_ext, '.txt'), ['No frequencies found'], fmt='%s')
        p_orb_i = [0, 0]
        const_i = [const_1, const_2]
        slope_i = [slope_1, slope_2]
        f_n_i = [f_n_1, f_n_2]
        a_n_i = [a_n_1, a_n_2]
        ph_n_i = [ph_n_1, ph_n_2]
        return p_orb_i, const_i, slope_i, f_n_i, a_n_i, ph_n_i
    # -------------------------------------------------------------------------------
    # [3] --- measure the orbital period with pdm and couple the harmonic frequencies
    # -------------------------------------------------------------------------------
    file_name_3 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_3.hdf5')
    fn_ext = os.path.splitext(os.path.basename(file_name_3))[1]
    if os.path.isfile(file_name_3) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_3, verbose=verbose)
        p_orb_3, const_3, slope_3, f_n_3, a_n_3, ph_n_3 = results
        p_orb_3 = p_orb_3[0]  # must be a float
        model_3 = tsf.linear_curve(times, const_3, slope_3, i_sectors)
        model_3 += tsf.sum_sines(times, f_n_3, a_n_3, ph_n_3)
        if verbose:
            print(f'Step 3: Loaded existing results\n')
    elif os.path.isfile(file_name_3.replace(fn_ext, '.txt')) & (not overwrite):  # p_orb too long last time
        p_orb_i = [0, 0]
        const_i = [const_1, const_2]
        slope_i = [slope_1, slope_2]
        f_n_i = [f_n_1, f_n_2]
        a_n_i = [a_n_1, a_n_2]
        ph_n_i = [ph_n_1, ph_n_2]
        if verbose:
            print(f'Step 3: Period over time-base is less than two\n')
        return p_orb_i, const_i, slope_i, f_n_i, a_n_i, ph_n_i
    else:
        if verbose:
            print(f'Step 3: Coupling the harmonic frequencies to the orbital frequency.')
        t_3a = time.time()
        if (p_orb == 0):
            p_orb_3 = frequency_analysis_porb(times, signal, f_n_2, a_n_2, ph_n_2, noise_level_2)
        else:
            # else we use the input p_orb at face value
            p_orb_3 = p_orb
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_2, p_orb_3, f_tol=freq_res/2)
        # if time-series too short, or no harmonics found, warn and cut off the analysis
        if (np.ptp(times) / p_orb_3 < 2):
            if verbose:
                print(f'Period over time-base is less than two: {np.ptp(times) / p_orb_3}')
            # save
            col1 = ['Period over time-base is less than two:', 'period (days)', 'time-base (days)']
            col2 = [np.ptp(times) / p_orb_3, p_orb_3, np.ptp(times)]
            np.savetxt(file_name_3.replace(fn_ext, '.txt'), np.column_stack((col1, col2)), fmt='%s')
            # return
            if (np.ptp(times) / p_orb_3 < 1.1):
                p_orb_i = [0, 0, p_orb_3]
                const_i = [const_1, const_2, const_2]
                slope_i = [slope_1, slope_2, slope_2]
                f_n_i = [f_n_1, f_n_2, f_n_2]
                a_n_i = [a_n_1, a_n_2, a_n_2]
                ph_n_i = [ph_n_1, ph_n_2, ph_n_2]
                return p_orb_i, const_i, slope_i, f_n_i, a_n_i, ph_n_i
        elif (len(harmonics) < 2):
            if verbose:
                print(f'Not enough harmonics found: {len(harmonics)}')
            # save and return
            col1 = ['Not enough harmonics found:', 'period (days)', 'time-base (days)']
            col2 = [len(harmonics), p_orb_3, np.ptp(times)]
            np.savetxt(file_name_3.replace(fn_ext, '.txt'), np.column_stack((col1, col2)), fmt='%s')
            p_orb_i = [0, 0, p_orb_3]
            const_i = [const_1, const_2, const_2]
            slope_i = [slope_1, slope_2, slope_2]
            f_n_i = [f_n_1, f_n_2, f_n_2]
            a_n_i = [a_n_1, a_n_2, a_n_2]
            ph_n_i = [ph_n_1, ph_n_2, ph_n_2]
            return p_orb_i, const_i, slope_i, f_n_i, a_n_i, ph_n_i
        # now couple the harmonics to the period. likely removes more frequencies that need re-extracting
        out_3 = tsf.fix_harmonic_frequency(times, signal, p_orb_3, const_2, slope_2, f_n_2, a_n_2, ph_n_2, i_sectors)
        t_3b = time.time()
        # main function done, do the rest for this step
        const_3, slope_3, f_n_3, a_n_3, ph_n_3 = out_3
        model_3 = tsf.linear_curve(times, const_3, slope_3, i_sectors)
        model_3 += tsf.sum_sines(times, f_n_3, a_n_3, ph_n_3)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_3, p_orb_3, f_tol=1e-9)
        n_param_3 = 2 * n_sectors + 1 + 2 * len(harmonics) + 3 * (len(f_n_3) - len(harmonics))
        bic_3 = tsf.calc_bic((signal - model_3)/signal_err, n_param_3)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48mOrbital harmonic frequencies coupled. Period: {p_orb_3:2.4}\033[0m')
            print(f'\033[0;32;48m{len(f_n_3)} frequencies, {n_param_3} free parameters. '
                  f'BIC: {bic_3:1.2f}, time taken: {t_3b - t_3a:1.1f}s\033[0m\n')
        # save
        results = (p_orb_3, const_3, slope_3, f_n_3, a_n_3, ph_n_3)
        f_errors = tsf.formal_uncertainties(times, signal - model_3, a_n_3, i_sectors)
        c_err_3, sl_err_3, f_n_err_3, a_n_err_3, ph_n_err_3 = f_errors
        p_err_3 = tsf.formal_period_uncertainty(p_orb_3, f_n_err_3, harmonics, harmonic_n)
        errors = (p_err_3, c_err_3, sl_err_3, f_n_err_3, a_n_err_3, ph_n_err_3)
        stats = (n_param_3, bic_3, np.std(signal - model_3))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_3.hdf5')
        desc = '[3] Harmonic frequencies coupled.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
    f_errors = tsf.formal_uncertainties(times, signal - model_2, a_n_2, i_sectors)
    c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2 = f_errors
    # ----------------------------------------------------------------------
    # [4] --- attempt to extract more harmonics knowing where they should be
    # ----------------------------------------------------------------------
    file_name_4 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_4.hdf5')
    if os.path.isfile(file_name_4) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_4, verbose=verbose)
        p_orb_4, const_4, slope_4, f_n_4, a_n_4, ph_n_4 = results
        n_param_4, bic_4, noise_level_4 = stats
        model_4 = tsf.linear_curve(times, const_4, slope_4, i_sectors)
        model_4 += tsf.sum_sines(times, f_n_4, a_n_4, ph_n_4)
        if verbose:
            print(f'Step 4: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 4: Looking for additional harmonics.')
        t_4a = time.time()
        out_4 = tsf.extract_additional_harmonics(times, signal, signal_err, p_orb_3, const_3, slope_3,
                                                 f_n_3, a_n_3, ph_n_3, i_sectors, verbose=verbose)
        t_4b = time.time()
        # main function done, do the rest for this step
        const_4, slope_4, f_n_4, a_n_4, ph_n_4 = out_4
        model_4 = tsf.linear_curve(times, const_4, slope_4, i_sectors)
        model_4 += tsf.sum_sines(times, f_n_4, a_n_4, ph_n_4)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_4, p_orb_3, f_tol=1e-9)
        n_param_4 = 2 * n_sectors + 1 + 2 * len(harmonics) + 3 * (len(f_n_4) - len(harmonics))
        bic_4 = tsf.calc_bic((signal - model_4)/signal_err, n_param_4)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48m{len(f_n_4) - len(f_n_3)} additional harmonics added.\033[0m')
            print(f'\033[0;32;48m{len(f_n_4)} frequencies, {n_param_4} free parameters. '
                  f'BIC: {bic_4:1.2f}, time taken: {t_4b - t_4a:1.1f}s\033[0m\n')
        # save
        results = (p_orb_3, const_4, slope_4, f_n_4, a_n_4, ph_n_4)
        f_errors = tsf.formal_uncertainties(times, signal - model_4, a_n_4, i_sectors)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_4, p_orb_3, f_tol=1e-9)
        c_err_4, sl_err_4, f_n_err_4, a_n_err_4, ph_n_err_4 = f_errors
        p_err_4 = tsf.formal_period_uncertainty(p_orb_3, f_n_err_4, harmonics, harmonic_n)
        errors = (p_err_4, c_err_4, sl_err_4, f_n_err_4, a_n_err_4, ph_n_err_4)
        stats = (n_param_4, bic_4, np.std(signal - model_4))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_4.hdf5')
        desc = '[4] Additional harmonic extraction.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
    # -----------------------------------------------------------------
    # [5] --- fit a second time but now with fixed harmonic frequencies
    # -----------------------------------------------------------------
    file_name_5 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_5.hdf5')
    if os.path.isfile(file_name_5) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_5, verbose=verbose)
        p_orb_5, const_5, slope_5, f_n_5, a_n_5, ph_n_5 = results
        p_orb_5 = p_orb_5[0]  # must be a float
        model_5 = tsf.linear_curve(times, const_5, slope_5, i_sectors)
        model_5 += tsf.sum_sines(times, f_n_5, a_n_5, ph_n_5)
        if verbose:
            print(f'Step 5: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 5: Starting multi-sine NL-LS fit with harmonics.')
        t_5a = time.time()
        out_5 = tsfit.fit_multi_sinusoid_harmonics_per_group(times, signal, signal_err, p_orb_3, const_4, slope_4,
                                                             f_n_4, a_n_4, ph_n_4, i_sectors, verbose=verbose)
        t_5b = time.time()
        # main function done, do the rest for this step
        p_orb_5, const_5, slope_5, f_n_5, a_n_5, ph_n_5 = out_5
        model_5 = tsf.linear_curve(times, const_5, slope_5, i_sectors)
        model_5 += tsf.sum_sines(times, f_n_5, a_n_5, ph_n_5)
        bic_5 = tsf.calc_bic((signal - model_5)/signal_err, n_param_4)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48mFit with fixed harmonics complete. Period: {p_orb_5:2.4}\033[0m')
            print(f'\033[0;32;48m{len(f_n_5)} frequencies, {n_param_4} free parameters. '
                  f'BIC: {bic_5:1.2f}, time taken: {t_5b - t_5a:1.1f}s\033[0m\n')
        # save
        results = (p_orb_5, const_5, slope_5, f_n_5, a_n_5, ph_n_5)
        f_errors = tsf.formal_uncertainties(times, signal - model_5, a_n_5, i_sectors)
        c_err_5, sl_err_5, f_n_err_5, a_n_err_5, ph_n_err_5 = f_errors
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_5, p_orb_5, f_tol=1e-9)
        p_err_5 = tsf.formal_period_uncertainty(p_orb_5, f_n_err_5, harmonics, harmonic_n)
        errors = (p_err_5, c_err_5, sl_err_5, f_n_err_5, a_n_err_5, ph_n_err_5)
        stats = (n_param_4, bic_5, np.std(signal - model_5))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_5.hdf5')
        desc = '[5] Multi-sine NL-LS fit results with coupled harmonics.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
    # --------------------------------------------------------------
    # [6] --- attempt to extract additional non-harmonic frequencies
    # --------------------------------------------------------------
    file_name_6 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_6.hdf5')
    if os.path.isfile(file_name_6) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_6, verbose=verbose)
        p_orb_6, const_6, slope_6, f_n_6, a_n_6, ph_n_6 = results
        n_param_6, bic_6, noise_level_6 = stats
        model_6 = tsf.linear_curve(times, const_6, slope_6, i_sectors)
        model_6 += tsf.sum_sines(times, f_n_6, a_n_6, ph_n_6)
        if verbose:
            print(f'Step 6: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 6: Looking for additional frequencies.')
        t_6a = time.time()
        out_6 = tsf.extract_additional_frequencies(times, signal, signal_err, p_orb_5, const_5, slope_5,
                                                   f_n_5, a_n_5, ph_n_5, i_sectors, verbose=verbose)
        t_6b = time.time()
        # main function done, do the rest for this step
        const_6, slope_6, f_n_6, a_n_6, ph_n_6 = out_6
        model_6 = tsf.linear_curve(times, const_6, slope_6, i_sectors)
        model_6 += tsf.sum_sines(times, f_n_6, a_n_6, ph_n_6)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_6, p_orb_5, f_tol=1e-9)
        n_param_6 = 2 * n_sectors + 1 + 2 * len(harmonics) + 3 * (len(f_n_6) - len(harmonics))
        bic_6 = tsf.calc_bic((signal - model_6)/signal_err, n_param_6)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48m{len(f_n_6) - len(f_n_5)} additional frequencies added.\033[0m')
            print(f'\033[0;32;48m{len(f_n_6)} frequencies, {n_param_6} free parameters. '
                  f'BIC: {bic_6:1.2f}, time taken: {t_6b - t_6a:1.1f}s\033[0m\n')
        # save
        results = (p_orb_5, const_6, slope_6, f_n_6, a_n_6, ph_n_6)
        f_errors = tsf.formal_uncertainties(times, signal - model_6, a_n_6, i_sectors)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_6, p_orb_5, f_tol=1e-9)
        c_err_6, sl_err_6, f_n_err_6, a_n_err_6, ph_n_err_6 = f_errors
        p_err_6 = tsf.formal_period_uncertainty(p_orb_5, f_n_err_6, harmonics, harmonic_n)
        errors = (p_err_6, c_err_6, sl_err_6, f_n_err_6, a_n_err_6, ph_n_err_6)
        stats = (n_param_6, bic_6, np.std(signal - model_6))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_6.hdf5')
        desc = '[6] Additional non-harmonic extraction.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
    # -------------------------------------------------------------------
    # [7] --- need to fit once more after the additon of some frequencies
    # -------------------------------------------------------------------
    file_name_7 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_7.hdf5')
    if os.path.isfile(file_name_7) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_7, verbose=verbose)
        p_orb_7, const_7, slope_7, f_n_7, a_n_7, ph_n_7 = results
        p_orb_7 = p_orb_7[0]  # must be a float
        model_7 = tsf.linear_curve(times, const_7, slope_7, i_sectors)
        model_7 += tsf.sum_sines(times, f_n_7, a_n_7, ph_n_7)
        if verbose:
            print(f'Step 7: Loaded existing results\n')
    else:
        if (len(f_n_5) < len(f_n_6)):
            if verbose:
                print(f'Step 7: Starting multi-sine NL-LS fit with harmonics.')
            t_7a = time.time()
            out_7 = tsfit.fit_multi_sinusoid_harmonics_per_group(times, signal, signal_err, p_orb_5, const_6, slope_6,
                                                                 f_n_6, a_n_6, ph_n_6, i_sectors, verbose=verbose)
            t_7b = time.time()
            # main function done, do the rest for this step
            p_orb_7, const_7, slope_7, f_n_7, a_n_7, ph_n_7 = out_7
            model_7 = tsf.linear_curve(times, const_7, slope_7, i_sectors)
            model_7 += tsf.sum_sines(times, f_n_7, a_n_7, ph_n_7)
            bic_7 = tsf.calc_bic((signal - model_7)/signal_err, n_param_6)
            # now print some useful info and save the result
            if verbose:
                print(f'\033[1;32;48mFit with fixed harmonics complete. Period: {p_orb_7:2.4}\033[0m')
                print(f'\033[0;32;48m{len(f_n_7)} frequencies, {n_param_6} free parameters. '
                      f'BIC: {bic_7:1.2f}, time taken: {t_7b - t_7a:1.1f}s\033[0m\n')
            # save
            results = (p_orb_7, const_7, slope_7, f_n_7, a_n_7, ph_n_7)
            f_errors = tsf.formal_uncertainties(times, signal - model_7, a_n_7, i_sectors)
            c_err_7, sl_err_7, f_n_err_7, a_n_err_7, ph_n_err_7 = f_errors
            harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_7, p_orb_7, f_tol=1e-9)
            p_err_7 = tsf.formal_period_uncertainty(p_orb_7, f_n_err_7, harmonics, harmonic_n)
            errors = (p_err_7, c_err_7, sl_err_7, f_n_err_7, a_n_err_7, ph_n_err_7)
            stats = (n_param_6, bic_7, np.std(signal - model_7))
            file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_7.hdf5')
            desc = '[7] Multi-sine NL-LS fit results with coupled harmonics.'
            ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
        else:
            p_orb_7, const_7, slope_7, f_n_7, a_n_7, ph_n_7 = p_orb_5, const_5, slope_5, f_n_5, a_n_5, ph_n_5
            model_7 = np.copy(model_5)
            bic_7 = tsf.calc_bic((signal - model_7)/signal_err, n_param_4)
            if verbose:
                print(f'\033[1;32;48mNo frequencies added, so no additional fit needed.\033[0m')
    # ----------------------------------------------------------------------
    # [8] --- try to reduce the number of frequencies after the fit was done
    # ----------------------------------------------------------------------
    harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_7, p_orb_7, f_tol=1e-9)
    file_name_8 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_8.hdf5')
    if os.path.isfile(file_name_8) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_8, verbose=verbose)
        p_orb_8, const_8, slope_8, f_n_8, a_n_8, ph_n_8 = results
        model_8 = tsf.linear_curve(times, const_8, slope_8, i_sectors)
        model_8 += tsf.sum_sines(times, f_n_8, a_n_8, ph_n_8)
        if verbose:
            print(f'Step 8: Loaded existing results\n')
    else:
        if verbose:
            print(f'Step 8: Attempting to reduce the number of frequencies.')
        t_8a = time.time()
        out_8 = tsf.reduce_frequencies_harmonics(times, signal, signal_err, p_orb_7, const_7, slope_7,
                                                 f_n_7, a_n_7, ph_n_7, i_sectors, verbose=verbose)
        t_8b = time.time()
        # main function done, do the rest for this step
        const_8, slope_8, f_n_8, a_n_8, ph_n_8 = out_8
        model_8 = tsf.linear_curve(times, const_8, slope_8, i_sectors)
        model_8 += tsf.sum_sines(times, f_n_8, a_n_8, ph_n_8)
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_8, p_orb_7, f_tol=1e-9)
        n_param_8 = 2 * n_sectors + 1 + 2 * len(harmonics) + 3 * (len(f_n_8) - len(harmonics))
        bic_8 = tsf.calc_bic((signal - model_8)/signal_err, n_param_8)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48mReducing frequencies complete.\033[0m')
            print(f'\033[0;32;48m{len(f_n_8)} frequencies, {n_param_8} free parameters. '
                  f'BIC: {bic_8:1.2f}, time taken: {t_8b - t_8a:1.1f}s\033[0m\n')
        # save
        results = (p_orb_7, const_8, slope_8, f_n_8, a_n_8, ph_n_8)
        f_errors = tsf.formal_uncertainties(times, signal - model_8, a_n_8, i_sectors)
        c_err_8, sl_err_8, f_n_err_8, a_n_err_8, ph_n_err_8 = f_errors
        p_err_8 = tsf.formal_period_uncertainty(p_orb_7, f_n_err_8, harmonics, harmonic_n)
        errors = (p_err_8, c_err_8, sl_err_8, f_n_err_8, a_n_err_8, ph_n_err_8)
        stats = (n_param_8, bic_8, np.std(signal - model_8))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_8.hdf5')
        desc = '[8] Reduce frequency set.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
    # -------------------------------------------------------------------
    # [9] --- need to fit once more after the removal of some frequencies
    # -------------------------------------------------------------------
    file_name_9 = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9.hdf5')
    if os.path.isfile(file_name_9) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_9, verbose=verbose)
        p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9 = results
        p_orb_9 = p_orb_9[0]  # must be a float
        model_9 = tsf.linear_curve(times, const_9, slope_9, i_sectors)
        model_9 += tsf.sum_sines(times, f_n_9, a_n_9, ph_n_9)
        if verbose:
            print(f'Step 9: Loaded existing results\n')
    else:
        if (len(f_n_7) > len(f_n_8)):
            if verbose:
                print(f'Step 9: Starting second multi-sine NL-LS fit with harmonics.')
            t_9a = time.time()
            out_9 = tsfit.fit_multi_sinusoid_harmonics_per_group(times, signal, signal_err, p_orb_7, const_8, slope_8,
                                                                 f_n_8, a_n_8, ph_n_8, i_sectors, verbose=verbose)
            t_9b = time.time()
            # main function done, do the rest for this step
            p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9 = out_9
            model_9 = tsf.linear_curve(times, const_9, slope_9, i_sectors)
            model_9 += tsf.sum_sines(times, f_n_9, a_n_9, ph_n_9)
            harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_9, p_orb_9, f_tol=1e-9)
            n_param_9 = 2 * n_sectors + 1 + 2 * len(harmonics) + 3 * (len(f_n_9) - len(harmonics))
            bic_9 = tsf.calc_bic((signal - model_9)/signal_err, n_param_9)
            # now print some useful info and/or save the result
            if verbose:
                print(f'\033[1;32;48mFit with fixed harmonics complete. Period: {p_orb_9:2.4}\033[0m')
                print(f'\033[0;32;48m{len(f_n_9)} frequencies, {n_param_9} free parameters. '
                      f'BIC: {bic_9:1.2f}, time taken: {t_9b - t_9a:1.1f}s\033[0m\n')
        else:
            p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9 = p_orb_7, const_7, slope_7, f_n_7, a_n_7, ph_n_7
            n_param_9 = n_param_6
            model_9 = np.copy(model_7)
            bic_9 = tsf.calc_bic((signal - model_9)/signal_err, n_param_9)
            if verbose:
                print(f'\033[1;32;48mNo frequencies removed, so no additional fit needed.\033[0m')
        # save
        results = (p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9)
        f_errors = tsf.formal_uncertainties(times, signal - model_9, a_n_9, i_sectors)
        c_err_9, sl_err_9, f_n_err_9, a_n_err_9, ph_n_err_9 = f_errors
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_9, p_orb_9, f_tol=1e-9)
        p_err_9 = tsf.formal_period_uncertainty(p_orb_9, f_n_err_9, harmonics, harmonic_n)
        errors = (p_err_9, c_err_9, sl_err_9, f_n_err_9, a_n_err_9, ph_n_err_9)
        stats = (n_param_9, bic_9, np.std(signal - model_9))
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9.hdf5')
        desc = '[9] Second multi-sine NL-LS fit results with coupled harmonics.'
        ut.save_results(results, errors, stats, file_name, description=desc, data_id=data_id)
        # save final freqs and linear curve in ascii format
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9_sinusoid.csv')
        data = np.column_stack((f_n_9, f_n_err_9, a_n_9, a_n_err_9, ph_n_9, ph_n_err_9))
        hdr = f'p_orb_9: {p_orb_9}, p_err_9: {p_err_9}\nf_n_9, f_n_err_9, a_n_9, a_n_err_9, ph_n_9, ph_n_err_9'
        np.savetxt(file_name, data, delimiter=',', header=hdr)
        file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9_linear.csv')
        data = np.column_stack((const_9, c_err_9, slope_9, sl_err_9, i_sectors[:, 0], i_sectors[:, 1]))
        hdr = (f'p_orb_9: {p_orb_9}, p_err_9: {p_err_9}\n'
               f'const_9, c_err_9, slope_9, sl_err_9, sector_start, sector_end')
        np.savetxt(file_name, data, delimiter=',', header=hdr)
    # final timing and message
    t_0b = time.time()
    if verbose:
        print(f'Frequency extraction done. Total time elapsed: {t_0b - t_0a:1.1f}s.\n')
    # make lists
    p_orb_i = [0, 0, p_orb_3, p_orb_3, p_orb_5, p_orb_5, p_orb_7, p_orb_7, p_orb_9]
    const_i = [const_1, const_2, const_3, const_4, const_5, const_6, const_7, const_8, const_9]
    slope_i = [slope_1, slope_2, slope_3, slope_4, slope_5, slope_6, slope_7, slope_8, slope_9]
    f_n_i = [f_n_1, f_n_2, f_n_3, f_n_4, f_n_5, f_n_6, f_n_7, f_n_8, f_n_9]
    a_n_i = [a_n_1, a_n_2, a_n_3, a_n_4, a_n_5, a_n_6, a_n_7, a_n_8, a_n_9]
    ph_n_i = [ph_n_1, ph_n_2, ph_n_3, ph_n_4, ph_n_5, ph_n_6, ph_n_7, ph_n_8, ph_n_9]
    return p_orb_i, const_i, slope_i, f_n_i, a_n_i, ph_n_i


def eclipse_analysis_timings(times, p_orb, f_n, a_n, ph_n, p_err, noise_level, file_name, data_id=None,
                             overwrite=False, verbose=False):
    """Takes the output of the frequency analysis and finds the position
    of the eclipses using the orbital harmonics

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    p_orb: float
        Orbital period of the eclipsing binary in days
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    p_err: float
        Error in the orbital period
    noise_level: float
        The noise level (standard deviation of the residuals)
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    t_zero: float, None
        Time of deepest minimum modulo p_orb
    timings: numpy.ndarray[float], None
        Eclipse timings of minima and first and last contact points,
        Eclipse timings of the possible flat bottom (internal tangency),
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    depths: numpy.ndarray[float], None
        Eclipse depth of the primary and secondary, depth_1, depth_2
    timing_errs: numpy.ndarray[float], None
        Error estimates for the eclipse timings,
        t_1_err, t_2_err, t_1_1_err, t_1_2_err, t_2_1_err, t_2_2_err
    depths_err: numpy.ndarray[float], None
        Error estimates for the depths
    ecl_indices: numpy.ndarray[int], None
        Indices of several important points in the harmonic model
        as generated here (see function for details)
    """
    t_a = time.time()
    fn_ext = os.path.splitext(os.path.basename(file_name))[1]
    file_name_2 = file_name.replace(fn_ext, '_ecl_indices' + fn_ext)
    file_name_3 = file_name.replace(fn_ext, '.txt')
    if os.path.isfile(file_name) & os.path.isfile(file_name_2) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        results = ut.read_results_timings(file_name)
        t_zero, timings, depths, timing_errs, depths_err, ecl_indices = results
    elif os.path.isfile(file_name_2) & (not overwrite):  # not enough eclipses found last time
        ecl_indices = ut.read_results_ecl_indices(file_name)  # read only the indices file
        if verbose:
            print(f'Not enough eclipses found last time. Loaded indices file.')
        return (None,) * 5 + (ecl_indices,)
    elif os.path.isfile(file_name_3) & (not overwrite):
        if verbose:
            print(f'Not enough eclipses found last time (see {os.path.splitext(os.path.basename(file_name_3))[0]})')
        return (None,) * 6
    else:
        if verbose:
            print(f'Measuring eclipse time points and depths.')
        # find any gaps in phase coverage
        t_fold_edges = times % p_orb
        if np.all(t_fold_edges > 0):
            t_fold_edges = np.append([0], t_fold_edges)
        if np.all(t_fold_edges < p_orb):
            t_fold_edges = np.append(t_fold_edges, [p_orb])
        t_gaps = tsf.mark_folded_gaps(t_fold_edges, p_orb/100)
        t_gaps = np.vstack((t_gaps, t_gaps + p_orb))  # duplicate for interval [0, 2p]
        # we use the lowest harmonics
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n, p_orb, f_tol=1e-9)
        low_h = (harmonic_n <= 20)  # restrict harmonics to avoid interference of ooe signal
        f_h, a_h, ph_h = f_n[harmonics], a_n[harmonics], ph_n[harmonics]
        # measure eclipse timings - deepest eclipse is put first in each measurement
        output = af.measure_eclipses_dt(p_orb, f_h[low_h], a_h[low_h], ph_h[low_h], noise_level, t_gaps)
        t_zero, t_1, t_2, t_contacts, depths, t_tangency, t_i_1_err, t_i_2_err, ecl_indices = output
        # if at first we don't succeed, try all harmonics
        if np.any([item is None for item in output]):
            output = af.measure_eclipses_dt(p_orb, f_h, a_h, ph_h, noise_level, t_gaps)
            t_zero, t_1, t_2, t_contacts, depths, t_tangency, t_i_1_err, t_i_2_err, ecl_indices = output
        # account for not finding eclipses
        if np.all([item is None for item in output]):
            message = f'No eclipse signatures found above the noise level of {noise_level}'
            np.savetxt(file_name.replace(fn_ext, '.txt'), [message], fmt='%s')
            if verbose:
                print(message)
            return (None,) * 6
        elif np.any([item is None for item in output]):
            message = 'No two eclipses found passing the criteria'
            np.savetxt(file_name.replace(fn_ext, '.txt'), [message], fmt='%s')
            ut.save_results_ecl_indices(ecl_indices, file_name, data_id=data_id)
            if verbose:
                print(message)
            return (None,) * 5 + (ecl_indices,)
        # minima and first/last contact and internal tangency
        timings = np.array([t_1, t_2, *t_contacts, *t_tangency])
        # define some errors
        t_1_err = np.sqrt(t_i_1_err[0]**2 + t_i_2_err[0]**2 + p_err**2) / 3  # this is an estimate
        t_2_err = np.sqrt(t_i_1_err[1]**2 + t_i_2_err[1]**2 + p_err**2) / 3  # this is an estimate
        timing_errs = np.array([t_1_err, t_2_err, t_i_1_err[0], t_i_2_err[0], t_i_1_err[1], t_i_2_err[1]])
        # depth errors from the noise levels at contact points and bottom of eclipse
        # sqrt(std(resid)**2/4+std(resid)**2/4+std(resid)**2)
        depths_err = np.array([np.sqrt(3 / 2 * noise_level**2), np.sqrt(3 / 2 * noise_level**2)])
        # save
        ut.save_results_timings(t_zero, timings, depths, timing_errs, depths_err, ecl_indices, file_name, data_id)
    # total durations
    dur_1 = timings[3] - timings[2]  # t_1_2 - t_1_1
    dur_2 = timings[5] - timings[4]  # t_2_2 - t_2_1
    dur_b_1 = timings[7] - timings[6]  # t_b_1_2 - t_b_1_1
    dur_b_2 = timings[9] - timings[8]  # t_b_2_2 - t_b_2_1
    dur_1_err = np.sqrt(timing_errs[2]**2 + timing_errs[3]**2)
    dur_2_err = np.sqrt(timing_errs[4]**2 + timing_errs[5]**2)
    t_b = time.time()
    if verbose:
        # determine decimals to print for two significant figures
        rnd_p_orb = max(ut.decimal_figures(p_err, 2), ut.decimal_figures(p_orb, 2))
        rnd_t_zero = max(ut.decimal_figures(timing_errs[0], 2), ut.decimal_figures(t_zero, 2))
        rnd_t_1 = max(ut.decimal_figures(timing_errs[0], 2), ut.decimal_figures(timings[0], 2))
        rnd_t_2 = max(ut.decimal_figures(timing_errs[1], 2), ut.decimal_figures(timings[1], 2))
        rnd_t_1_1 = max(ut.decimal_figures(timing_errs[2], 2), ut.decimal_figures(timings[2], 2))
        rnd_t_1_2 = max(ut.decimal_figures(timing_errs[3], 2), ut.decimal_figures(timings[3], 2))
        rnd_t_2_1 = max(ut.decimal_figures(timing_errs[4], 2), ut.decimal_figures(timings[4], 2))
        rnd_t_2_2 = max(ut.decimal_figures(timing_errs[5], 2), ut.decimal_figures(timings[5], 2))
        rnd_dur_1 = max(ut.decimal_figures(dur_1_err, 2), ut.decimal_figures(dur_1, 2))
        rnd_dur_2 = max(ut.decimal_figures(dur_2_err, 2), ut.decimal_figures(dur_2, 2))
        rnd_d_1 = max(ut.decimal_figures(depths_err[0], 2), ut.decimal_figures(depths[0], 2))
        rnd_d_2 = max(ut.decimal_figures(depths_err[1], 2), ut.decimal_figures(depths[1], 2))
        rnd_bot_1 = max(ut.decimal_figures(dur_1_err, 2), ut.decimal_figures(dur_b_1, 2))
        rnd_bot_2 = max(ut.decimal_figures(dur_2_err, 2), ut.decimal_figures(dur_b_2, 2))
        print(f'\033[1;32;48mMeasurements of timings and depths:\033[0m')
        print(f'\033[0;32;48mp_orb: {p_orb:.{rnd_p_orb}f} (+-{p_err:.{rnd_t_1}f}), '
              f't_zero: {t_zero:.{rnd_t_zero}f} (+-{timing_errs[0]:.{rnd_t_zero}f}), \n'
              f't_1: {timings[0]:.{rnd_t_1}f} (+-{timing_errs[0]:.{rnd_t_1}f}), '
              f't_2: {timings[1]:.{rnd_t_2}f} (+-{timing_errs[1]:.{rnd_t_2}f}), \n'
              f't_1_1: {timings[2]:.{rnd_t_1_1}f} (+-{timing_errs[2]:.{rnd_t_1_1}f}), \n'
              f't_1_2: {timings[3]:.{rnd_t_1_2}f} (+-{timing_errs[3]:.{rnd_t_1_2}f}), \n'
              f't_2_1: {timings[4]:.{rnd_t_2_1}f} (+-{timing_errs[4]:.{rnd_t_2_1}f}), \n'
              f't_2_2: {timings[5]:.{rnd_t_2_2}f} (+-{timing_errs[5]:.{rnd_t_2_2}f}), \n'
              f'duration_1: {dur_1:.{rnd_dur_1}f} (+-{dur_1_err:.{rnd_dur_1}f}), \n'
              f'duration_2: {dur_2:.{rnd_dur_2}f} (+-{dur_2_err:.{rnd_dur_2}f}). \n'
              f't_b_1_1: {timings[6]:.{rnd_t_1_1}f} (+-{timing_errs[2]:.{rnd_t_1_1}f}), \n'
              f't_b_1_2: {timings[7]:.{rnd_t_1_2}f} (+-{timing_errs[3]:.{rnd_t_1_2}f}), \n'
              f't_b_2_1: {timings[8]:.{rnd_t_2_1}f} (+-{timing_errs[4]:.{rnd_t_2_1}f}), \n'
              f't_b_2_2: {timings[9]:.{rnd_t_2_2}f} (+-{timing_errs[5]:.{rnd_t_2_2}f}), \n'
              f'bottom_dur_1: {dur_b_1:.{rnd_bot_1}f} (+-{dur_1_err:.{rnd_bot_1}f}), \n'
              f'bottom_dur_2: {dur_b_2:.{rnd_bot_2}f} (+-{dur_2_err:.{rnd_bot_2}f}). \n'
              f'd_1: {depths[0]:.{rnd_d_1}f} (+-{depths_err[0]:.{rnd_d_1}f}), '
              f'd_2: {depths[1]:.{rnd_d_2}f} (+-{depths_err[1]:.{rnd_d_2}f}). \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return t_zero, timings, depths, timing_errs, depths_err, ecl_indices


def eclipse_analysis_cubics(times, signal, signal_err, p_orb, t_zero, timings, const, slope, f_n, a_n, ph_n,
                            i_sectors, file_name, data_id=None, overwrite=False, verbose=False):
    """Refine the eclispe timings using an empirical model of cubic functions

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    timings: numpy.ndarray[float]
        Eclipse timings of minima and first and last contact points,
        Eclipse timings of the possible flat bottom (internal tangency),
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    t_zero_em: float
        Time of deepest minimum modulo p_orb
    timings_em: numpy.ndarray[float]
        Eclipse timings from the empirical model.
        Timings of minima and first and last contact points,
        timings of the possible flat bottom (internal tangency,
        note that these may sometimes surpass the eclipse middle).
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    depths_em: numpy.ndarray[float]
        Cubic curve height difference between local extrema,
        will not always correspond to primary and secondary eclipse depth
    timings_em: numpy.ndarray[float]
        Eclipse timings from the empirical model.
        Timings of minima and first and last contact points,
        timings of the possible flat bottom (internal tangency.
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    depths_em: numpy.ndarray[float]
        Cubic curve primary and secondary eclipse depth

    Notes
    -----
    Eclipses are modelled by a simple empirical model consisting of
    one cubic funciton per eclipse, which is mirrored to both sides.
    Only the part of the cubic function between the two local
    extrema is used (so the discriminant is always positive).
    """
    t_a = time.time()
    # file needs to exist
    if os.path.isfile(file_name) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        t_zero_em, timings_em, depths_em = ut.read_results_cubics(file_name)
    else:
        if verbose:
            print(f'Improving timings and depths with cubic model')
        harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n, p_orb, f_tol=1e-9)
        low_h = (harmonic_n <= 20)
        # get the initial cubic polynomials from the low harmonic model timings
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2, t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2 = timings
        t_ys = np.array([t_1_1, t_b_1_1, t_2_1, t_b_2_1]) + t_zero  # add t_zero for the harmonic model
        y1, y2, y3, y4 = tsf.sum_sines(t_ys, f_n[harmonics[low_h]], a_n[harmonics[low_h]], ph_n[harmonics[low_h]])
        # fit for the cubic model parameters
        depths = np.array([y1-y2, y3-y4])
        output_a = tsfit.fit_eclipse_cubic(times, signal, signal_err, p_orb, t_zero, timings, depths, const, slope,
                                           f_n, a_n, ph_n, i_sectors, verbose=verbose)
        mid_1, mid_2, t_c1_1, t_c3_1, t_c1_2, t_c3_2, d_1, d_2 = output_a.x
        # get the rest of the timings of the cubic models and translate them
        t_c2_1, t_c2_2 = 2 * mid_1 - t_c1_1, 2 * mid_1 - t_c1_2
        t_c4_1, t_c4_2 = 2 * mid_2 - t_c3_1, 2 * mid_2 - t_c3_2
        t_zero_em = t_zero + mid_1  # shift everything so that mid_1 is zero
        depths_em = np.array([d_1, d_2])
        # adjust the parameters to more physical measures
        timings_em = np.array([mid_1, mid_2, t_c1_1, t_c2_1, t_c3_1, t_c4_1, t_c1_2, t_c2_2, t_c3_2, t_c4_2])
        timings_em[6:10] = [min(t_c1_2, mid_1), max(t_c2_2, mid_1), min(t_c3_2, mid_2), max(t_c4_2, mid_2)]
        timings_em = timings_em - mid_1
        # save
        ut.save_results_cubics(p_orb, t_zero_em, timings_em, depths_em, file_name, data_id=data_id)
    t_b = time.time()
    if verbose:
        # determine decimals to print for two significant figures
        dur_1, dur_2 = (timings_em[3] - timings_em[2]), (timings_em[5] - timings_em[4])
        dur_b_1, dur_b_2 = (timings_em[7] - timings_em[6]), (timings_em[9] - timings_em[8])
        rnd_t_zero = ut.decimal_figures(t_zero_em, 2)
        rnd_t_1 = ut.decimal_figures(timings_em[0], 2)
        rnd_t_2 = ut.decimal_figures(timings_em[1], 2)
        rnd_t_1_1 = ut.decimal_figures(timings_em[2], 2)
        rnd_t_1_2 = ut.decimal_figures(timings_em[3], 2)
        rnd_t_2_1 = ut.decimal_figures(timings_em[4], 2)
        rnd_t_2_2 = ut.decimal_figures(timings_em[5], 2)
        rnd_dur_1 = ut.decimal_figures(dur_1, 2)
        rnd_dur_2 = ut.decimal_figures(dur_2, 2)
        rnd_bot_1 = ut.decimal_figures(dur_b_1, 2)
        rnd_bot_2 = ut.decimal_figures(dur_b_2, 2)
        rnd_d_1 = ut.decimal_figures(depths_em[0], 2)
        rnd_d_2 = ut.decimal_figures(depths_em[1], 2)
        print(f'\033[1;32;48mOptimised empirical cubic model:\033[0m')
        print(f'\033[0;32;48mt_zero: {t_zero_em:.{rnd_t_zero}f}, '
              f't_1: {timings_em[0]:.{rnd_t_1}f}, t_2: {timings_em[1]:.{rnd_t_2}f}, \n'
              f't_1_1: {timings_em[2]:.{rnd_t_1_1}f}, t_1_2: {timings_em[3]:.{rnd_t_1_2}f}, \n'
              f't_2_1: {timings_em[4]:.{rnd_t_2_1}f}, t_2_2: {timings_em[5]:.{rnd_t_2_2}f}, \n'
              f'duration_1: {dur_1:.{rnd_dur_1}f}, duration_2: {dur_2:.{rnd_dur_2}f}. \n'
              f't_b_1_1: {timings_em[6]:.{rnd_t_1_1}f}, t_b_1_2: {timings_em[7]:.{rnd_t_1_2}f}, \n'
              f't_b_2_1: {timings_em[8]:.{rnd_t_2_1}f}, t_b_2_2: {timings_em[9]:.{rnd_t_2_2}f}, \n'
              f'bottom_dur_1: {dur_b_1:.{rnd_bot_1}f}, bottom_dur_2: {dur_b_2:.{rnd_bot_2}f}. \n'
              f'd_1: {depths_em[0]:.{rnd_d_1}f}, d_2: {depths_em[1]:.{rnd_d_2}f}. \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return t_zero_em, timings_em, depths_em


def eclipse_analysis_timing_err(times, signal, signal_err, p_orb, t_zero, timings, const, slope, f_n, a_n, ph_n, p_err,
                                noise_level, i_sectors, file_name, data_id=None, overwrite=False, verbose=False):
    """Takes the output of the frequency analysis and finds the position
    of the eclipses using the orbital harmonics

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    timings: numpy.ndarray[float]
        Eclipse timings of minima and first and last contact points,
        Eclipse timings of the possible flat bottom (internal tangency),
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    p_err: float
        Error in the orbital period
    noise_level: float
        The noise level (standard deviation of the residuals)
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    timing_errs: numpy.ndarray[float], None
        Error estimates for the eclipse timings,
        t_1_err, t_2_err, t_1_1_err, t_1_2_err, t_2_1_err, t_2_2_err
    depths: numpy.ndarray[float], None
        Eclipse depth of the primary and secondary, depth_1, depth_2
    depths_err: numpy.ndarray[float], None
        Error estimates for the depths
    """
    t_a = time.time()
    if os.path.isfile(file_name) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        results = ut.read_results_t_errors(file_name)
        timings, timing_errs, depths, depths_err = results
    else:
        if verbose:
            print('Improving timing and depth errors.')
        out_a = tsf.measure_timing_error(times, signal, p_orb, t_zero, const, slope, f_n, a_n, ph_n,
                                         timings, noise_level, i_sectors)
        t_1_1_err, t_1_2_err, t_2_1_err, t_2_2_err = out_a
        t_1_err = np.sqrt(t_1_1_err**2 + t_1_2_err**2) / 2
        t_2_err = np.sqrt(t_2_1_err**2 + t_2_2_err**2) / 2
        timing_errs = np.array([t_1_err, t_2_err, t_1_1_err, t_1_2_err, t_2_1_err, t_2_2_err])
        out_b = tsf.measure_eclipse_depths(times, signal, p_orb, t_zero, const, slope, f_n, a_n, ph_n, timings,
                                           timing_errs, noise_level, i_sectors)
        depth_1, depth_2, depth_1_err, depth_2_err = out_b
        depths = np.array([depth_1, depth_2])
        depths_err = np.array([depth_1_err, depth_2_err])
        # save
        ut.save_results_t_errors(timings, timing_errs, depths, depths_err, file_name, data_id)
    t_b = time.time()
    if verbose:
        # determine decimals to print for two significant figures
        rnd_t_zero = max(ut.decimal_figures(timing_errs[0], 2), ut.decimal_figures(t_zero, 2))
        rnd_t_1 = max(ut.decimal_figures(timing_errs[0], 2), ut.decimal_figures(timings[0], 2))
        rnd_t_2 = max(ut.decimal_figures(timing_errs[1], 2), ut.decimal_figures(timings[1], 2))
        rnd_t_1_1 = max(ut.decimal_figures(timing_errs[2], 2), ut.decimal_figures(timings[2], 2))
        rnd_t_1_2 = max(ut.decimal_figures(timing_errs[3], 2), ut.decimal_figures(timings[3], 2))
        rnd_t_2_1 = max(ut.decimal_figures(timing_errs[4], 2), ut.decimal_figures(timings[4], 2))
        rnd_t_2_2 = max(ut.decimal_figures(timing_errs[5], 2), ut.decimal_figures(timings[5], 2))
        rnd_d_1 = max(ut.decimal_figures(depths_err[0], 2), ut.decimal_figures(depths[0], 2))
        rnd_d_2 = max(ut.decimal_figures(depths_err[1], 2), ut.decimal_figures(depths[1], 2))
        print(f'\033[1;32;48mMeasurements of timings and depths:\033[0m')
        print(f'\033[0;32;48mt_zero: {t_zero:.{rnd_t_zero}f} (+-{timing_errs[0]:.{rnd_t_zero}f}), \n'
              f't_1: {timings[0]:.{rnd_t_1}f} (+-{timing_errs[0]:.{rnd_t_1}f}), '
              f't_2: {timings[1]:.{rnd_t_2}f} (+-{timing_errs[1]:.{rnd_t_2}f}), \n'
              f't_1_1: {timings[2]:.{rnd_t_1_1}f} (+-{timing_errs[2]:.{rnd_t_1_1}f}), '
              f't_1_2: {timings[3]:.{rnd_t_1_2}f} (+-{timing_errs[3]:.{rnd_t_1_2}f}), \n'
              f't_2_1: {timings[4]:.{rnd_t_2_1}f} (+-{timing_errs[4]:.{rnd_t_2_1}f}), '
              f't_2_2: {timings[5]:.{rnd_t_2_2}f} (+-{timing_errs[5]:.{rnd_t_2_2}f}), \n'
              f'd_1: {depths[0]:.{rnd_d_1}f} (+-{depths_err[0]:.{rnd_d_1}f}), '
              f'd_2: {depths[1]:.{rnd_d_2}f} (+-{depths_err[1]:.{rnd_d_2}f}), \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return timing_errs, depths, depths_err


def eclipse_analysis_elements(p_orb, t_zero, timings, depths, p_err, timing_errs, depths_err, f_h, a_h,
                              ph_h, file_name, data_id=None, overwrite=False, verbose=False):
    """Obtains orbital elements from the eclispe timings

    Parameters
    ----------
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    timings: numpy.ndarray[float]
        Eclipse timings of minima and first and last contact points,
        Timings of the possible flat bottom (internal tangency),
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
        t_b_1_1, t_b_1_2, t_b_2_1, t_b_2_2
    depths: numpy.ndarray[float]
        Eclipse depth of the primary and secondary, depth_1, depth_2
    p_err: float
        Error in the orbital period
    timing_errs: numpy.ndarray[float]
        Error estimates for the eclipse timings,
        t_1_err, t_2_err, t_1_1_err, t_1_2_err, t_2_1_err, t_2_2_err
    depths_err: numpy.ndarray[float]
        Error estimates for the depths
    f_h: numpy.ndarray[float]
        The frequencies of a number of harmonic sine waves
    a_h: numpy.ndarray[float]
        The amplitudes of a number of harmonic sine waves
    ph_h: numpy.ndarray[float]
        The phases of a number of harmonic sine waves
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    e: float
        Eccentricity of the orbit
    w: float
        Argument of periastron
    i: float
        Inclination of the orbit
    r_sum_sma: float
        Sum of radii in units of the semi-major axis
    r_ratio: float
        Radius ratio r_2/r_1
    sb_ratio: float
        Surface brightness ratio sb_2/sb_1
    errors: tuple[numpy.ndarray[float]]
        The (non-symmetric) errors for the same parameters as intervals.
        Derived from the intervals
    intervals: tuple[numpy.ndarray[float]]
        The HDIs (hdi_prob=0.683) for the parameters:
        e, w, i, phi_0, psi_0, r_sum_sma, r_dif_sma, r_ratio,
        sb_ratio, e*cos(w), e*sin(w), f_c, f_s
    bounds: tuple[numpy.ndarray[float]]
        The HDIs (hdi_prob=0.997) for the same parameters as intervals
    formal_errors: tuple[float]
        Formal (symmetric) errors in the parameters:
        e, w, phi_0, r_sum_sma, ecosw, esinw, f_c, f_s
    dists_in: tuple[numpy.ndarray[float]]
        Full input distributions for: t_1, t_2,
        tau_1_1, tau_1_2, tau_2_1, tau_2_2, d_1, d_2, bot_1, bot_2
    dists_out: tuple[numpy.ndarray[float]]
        Full output distributions for the same parameters as intervals
    """
    t_a = time.time()
    # opens two files so both need to exist
    fn_ext = os.path.splitext(os.path.basename(file_name))[1]
    file_name_2 = file_name.replace(fn_ext, '_dists' + fn_ext)
    if os.path.isfile(file_name) & os.path.isfile(file_name_2) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        results = ut.read_results_elements(file_name)
        e, w, i, r_sum_sma, r_ratio, sb_ratio = results[:6]
        errors, bounds, formal_errors, dists_in, dists_out = results[6:]
    else:
        if verbose:
            print('Determining eclipse parameters and error estimates.')
        # convert to durations
        tau_1_1 = timings[0] - timings[2]  # t_1 - t_1_1
        tau_1_2 = timings[3] - timings[0]  # t_1_2 - t_1
        tau_2_1 = timings[1] - timings[4]  # t_2 - t_2_1
        tau_2_2 = timings[5] - timings[1]  # t_2_2 - t_2
        timings_tau = np.array([timings[0], timings[1], tau_1_1, tau_1_2, tau_2_1, tau_2_2])
        tau_b_1_1 = timings[0] - timings[6]  # t_1 - t_b_1_1
        tau_b_1_2 = timings[7] - timings[0]  # t_b_1_2 - t_1
        tau_b_2_1 = timings[1] - timings[8]  # t_2 - t_b_2_1
        tau_b_2_2 = timings[9] - timings[1]  # t_b_2_2 - t_2
        timings_tau = np.append(timings_tau, [tau_b_1_1, tau_b_1_2, tau_b_2_1, tau_b_2_2])
        # minimisation procedure for parameters from formulae
        output = af.eclipse_parameters(p_orb, timings_tau, depths, timing_errs, depths_err, verbose=verbose)
        e, w, i, r_sum_sma, r_ratio, sb_ratio = output
        # calculate the errors
        output_2 = af.error_estimates_hdi(e, w, i, r_sum_sma, r_ratio, sb_ratio, p_orb, t_zero, f_h, a_h, ph_h,
                                          timings, timing_errs, depths_err, verbose=verbose)
        intervals, bounds, errors, dists_in, dists_out = output_2
        i_sym_err = max(errors[2])  # take the maximum as pessimistic estimate of the symmetric error
        formal_errors = af.formal_uncertainties(e, w, i, p_orb, *timings_tau[:6], p_err, i_sym_err, *timing_errs)
        # save
        ut.save_results_elements(e, w, i, r_sum_sma, r_ratio, sb_ratio, errors, intervals, bounds, formal_errors,
                                 dists_in, dists_out, file_name, data_id)
    t_b = time.time()
    if verbose:
        e_err, w_err, i_err, r_sum_sma_err, r_ratio_err, sb_ratio_err, ecosw_err, esinw_err, f_c_err, f_s_err = errors
        e_bds, w_bds, i_bds, r_sum_sma_bds, r_ratio_bds, sb_ratio_bds, ecosw_bds, esinw_bds, f_c_bds, f_s_bds = bounds
        # determine decimals to print for two significant figures
        rnd_e = max(ut.decimal_figures(min(e_err), 2), ut.decimal_figures(e, 2), 0)
        rnd_w = max(ut.decimal_figures(min(w_err) / np.pi * 180, 2), ut.decimal_figures(w / np.pi * 180, 2), 0)
        rnd_i = max(ut.decimal_figures(min(i_err) / np.pi * 180, 2), ut.decimal_figures(i / np.pi * 180, 2), 0)
        rnd_rsumsma = max(ut.decimal_figures(min(r_sum_sma_err), 2), ut.decimal_figures(r_sum_sma, 2), 0)
        rnd_rratio = max(ut.decimal_figures(min(r_ratio_err), 2), ut.decimal_figures(r_ratio, 2), 0)
        rnd_sbratio = max(ut.decimal_figures(min(sb_ratio_err), 2), ut.decimal_figures(sb_ratio, 2), 0)
        rnd_ecosw = max(ut.decimal_figures(min(ecosw_err), 2), ut.decimal_figures(e * np.cos(w), 2), 0)
        rnd_esinw = max(ut.decimal_figures(min(esinw_err), 2), ut.decimal_figures(e * np.sin(w), 2), 0)
        # multi interval
        w_bds, w_bds_2 = ut.bounds_multiplicity_check(w_bds, w)
        print(f'\033[1;32;48mMeasurements and initial optimisation of the eclipse parameters complete.\033[0m')
        print(f'\033[0;32;48me: {e:.{rnd_e}f} (+{e_err[1]:.{rnd_e}f} -{e_err[0]:.{rnd_e}f}), '
              f'bounds ({e_bds[0]:.{rnd_e}f}, {e_bds[1]:.{rnd_e}f}), \n'
              f'w: {w / np.pi * 180:.{rnd_w}f} '
              f'(+{w_err[1] / np.pi * 180:.{rnd_w}f} -{w_err[0] / np.pi * 180:.{rnd_w}f}) degrees, '
              f'bounds ({w_bds[0] / np.pi * 180:.{rnd_w}f}, {w_bds[1] / np.pi * 180:.{rnd_w}f}), \n'
              f'i: {i / np.pi * 180:.{rnd_i}f} '
              f'(+{i_err[1] / np.pi * 180:.{rnd_i}f} -{i_err[0] / np.pi * 180:.{rnd_i}f}) degrees, '
              f'bounds ({i_bds[0] / np.pi * 180:.{rnd_i}f}, {i_bds[1] / np.pi * 180:.{rnd_i}f}), \n'
              f'(r1+r2)/a: {r_sum_sma:.{rnd_rsumsma}f} '
              f'(+{r_sum_sma_err[1]:.{rnd_rsumsma}f} -{r_sum_sma_err[0]:.{rnd_rsumsma}f}), '
              f'bounds ({r_sum_sma_bds[0]:.{rnd_rsumsma}f}, {r_sum_sma_bds[1]:.{rnd_rsumsma}f}), \n'
              f'r2/r1: {r_ratio:.{rnd_rratio}f} (+{r_ratio_err[1]:.{rnd_rratio}f} -{r_ratio_err[0]:.{rnd_rratio}f}), '
              f'bounds ({r_ratio_bds[0]:.{rnd_rratio}f}, {r_ratio_bds[1]:.{rnd_rratio}f}), \n'
              f'sb2/sb1: {sb_ratio:.{rnd_sbratio}f} '
              f'(+{sb_ratio_err[1]:.{rnd_sbratio}f} -{sb_ratio_err[0]:.{rnd_sbratio}f}), '
              f'bounds ({sb_ratio_bds[0]:.{rnd_sbratio}f}, {sb_ratio_bds[1]:.{rnd_sbratio}f}), \n'
              f'ecos(w): {e * np.cos(w):.{rnd_ecosw}f} (+{ecosw_err[1]:.{rnd_ecosw}f} -{ecosw_err[0]:.{rnd_ecosw}f}), '
              f'bounds ({ecosw_bds[0]:.{rnd_ecosw}f}, {ecosw_bds[1]:.{rnd_ecosw}f}), \n'
              f'esin(w): {e * np.sin(w):.{rnd_esinw}f} (+{esinw_err[1]:.{rnd_esinw}f} -{esinw_err[0]:.{rnd_esinw}f}), '
              f'bounds ({esinw_bds[0]:.{rnd_esinw}f}, {esinw_bds[1]:.{rnd_esinw}f}). \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return e, w, i, r_sum_sma, r_ratio, sb_ratio, errors, bounds, formal_errors, dists_in, dists_out


def eclipse_analysis_fit(times, signal, signal_err, par_init, p_orb, t_zero, timings, const, slope, f_n, a_n, ph_n,
                         i_sectors, file_name, data_id=None, overwrite=False, verbose=False):
    """Obtains orbital elements from the eclispe timings

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    par_init: tuple[float], list[float], numpy.ndarray[float]
        Initial eclipse parameters to start the fit, consisting of:
        e, w, i, r_sum_sma, r_ratio, sb_ratio
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    timings: numpy.ndarray[float]
        Eclipse timings of minima and first and last contact points,
        t_1, t_2, t_1_1, t_1_2, t_2_1, t_2_2
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    par_opt_simple: tuple[float]
        Optimised eclipse parameters , consisting of:
        e, w, i, r_sum_sma, r_ratio, sb_ratio
    par_opt_ellc: tuple[float]
        Optimised eclipse parameters , consisting of:
        e, w, i, r_sum_sma, r_ratio, sb_ratio

    Notes
    -----
    The fit parameters for one of the fits include f_c=sqrt(e)cos(w) and
    f_s=sqrt(e)sin(w), instead of e and w themselves.
    """
    t_a = time.time()
    if os.path.isfile(file_name) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        par_init, par_opt_simple, par_opt_ellc  = ut.read_results_lc_fit(file_name)
        opt1_e, opt1_w, opt1_i, opt1_r_sum_sma, opt1_r_ratio, opt1_sb_ratio = par_opt_simple
        opt2_e, opt2_w, opt2_i, opt2_r_sum_sma, opt2_r_ratio, opt2_sb_ratio = par_opt_ellc
    else:
        if verbose:
            print('Fitting for the light curve parameters.')
        e, w = par_init[:2]
        e = min(e, 0.999)  # prevent unbound orbits
        par_init_simple = (e * np.cos(w), e * np.sin(w), *par_init[2:])
        output = tsfit.fit_simple_eclipse(times, signal, signal_err, p_orb, t_zero, timings, const, slope,
                                          f_n, a_n, ph_n, par_init_simple, i_sectors, verbose=verbose)
        # get e and w from fitting parameters
        opt1_ecosw, opt1_esinw, opt1_i, opt1_r_sum_sma, opt1_r_ratio, opt1_sb_ratio = output.x
        opt1_e = np.sqrt(opt1_ecosw**2 + opt1_esinw**2)
        opt1_w = np.arctan2(opt1_esinw, opt1_ecosw) % (2 * np.pi)
        par_opt_simple = (opt1_e, opt1_w, opt1_i, opt1_r_sum_sma, opt1_r_ratio, opt1_sb_ratio)
        # use results of first fit as initial values for the second fit
        par_init_ellc = (opt1_e**0.5 * np.cos(opt1_w), opt1_e**0.5 * np.sin(opt1_w), *par_opt_simple[2:])
        # todo: test with ldc_1=0.5 and 1.0 on the synthetics
        output = tsfit.fit_ellc_lc(times, signal, signal_err, p_orb, t_zero, timings, const, slope,
                                   f_n, a_n, ph_n, par_init_ellc, i_sectors, verbose=verbose)
        # todo: think of a way to get errors?
        # get e and w from fitting parameters f_c and f_s
        opt_f_c, opt_f_s, opt2_i, opt2_r_sum_sma, opt2_r_ratio, opt2_sb_ratio = output.x
        opt2_e = opt_f_c**2 + opt_f_s**2
        opt2_w = np.arctan2(opt_f_s, opt_f_c) % (2 * np.pi)
        par_opt_ellc = (opt2_e, opt2_w, opt2_i, opt2_r_sum_sma, opt2_r_ratio, opt2_sb_ratio)
        # save
        ut.save_results_lc_fit(par_init, par_opt_simple, par_opt_ellc, file_name, data_id)
    t_b = time.time()
    if verbose:
        print(f'\033[1;32;48mOptimisation of the light curve parameters complete.\033[0m')
        print(f'\033[0;32;48mInitial - e: {par_init[0]:2.4}, w: {par_init[1] / np.pi * 180:2.4} deg, '
              f'i: {par_init[2] / np.pi * 180:2.4} deg, (r1+r2)/a: {par_init[3]:2.4}, r2/r1: {par_init[4]:2.4}, '
              f'sb2/sb1: {par_init[5]:2.4}. \n'
              f'Simple fit - e: {opt1_e:2.4}, w: {opt1_w / np.pi * 180:2.4} deg, i: {opt1_i / np.pi * 180:2.4} deg, '
              f'(r1+r2)/a: {opt1_r_sum_sma:2.4}, r2/r1: {opt1_r_ratio:2.4}, sb2/sb1: {opt1_sb_ratio:2.4}. \n'
              f'ellc fit - e: {opt2_e:2.4}, w: {opt2_w / np.pi * 180:2.4} deg, i: {opt2_i / np.pi * 180:2.4} deg, '
              f'(r1+r2)/a: {opt2_r_sum_sma:2.4}, r2/r1: {opt2_r_ratio:2.4}, sb2/sb1: {opt2_sb_ratio:2.4}. \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return par_opt_simple, par_opt_ellc


def eclipse_analysis(tic, times, signal, signal_err, i_sectors, save_dir, data_id=None, overwrite=False, verbose=False):
    """Part two of analysis recipe for analysis of EB light curves,
    to be chained after frequency_analysis

    Parameters
    ----------
    tic: int
        The TESS Input Catalog number for later reference
        Use any number as reference if not available.
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_sectors = np.array([[0, len(times)]]).
    save_dir: str
        Path to a directory for saving the results. Also used to load
        previous analysis results.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    out_10: tuple
        output of eclipse_analysis_timings
    out_11: tuple
        output of eclipse_analysis_hsep
    out_12: tuple
        output of eclipse_analysis_timings
    out_13: tuple
        output of eclipse_analysis_elements
    out_14: tuple
        output of eclipse_analysis_fit
    """
    signal_err = np.max(signal_err) * np.ones(len(times))  # likelihood assumes the same errors
    # read in the frequency analysis results
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9.hdf5')
    if os.path.isfile(file_name):
        results, errors, stats = ut.read_results(file_name, verbose=verbose)
    else:
        if verbose:
            print('No prewhitening results found')
        return (None,) * 5
    p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9 = results
    p_orb_9 = p_orb_9[0]  # must be a float
    p_err_9, c_err_9, sl_err_9, f_n_err_9, a_n_err_9, ph_n_err_9 = errors
    n_param_9, bic_9, noise_level_9 = stats
    # --- [9] --- Initial eclipse timings
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_10.csv')
    out_10 = eclipse_analysis_timings(times, p_orb_9, f_n_9, a_n_9, ph_n_9, p_err_9, noise_level_9,
                                     file_name=file_name, data_id=data_id, overwrite=overwrite, verbose=verbose)
    t_zero_10, timings_10, depths_10, timing_errs_10, depths_err_10, ecl_indices_10 = out_10
    if np.any([item is None for item in out_10]):
        return (None,) * 5  # could still not find eclipses for some reason
    # --- [10] --- Improvement of timings with cubics
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_11.csv')
    out_11 = eclipse_analysis_cubics(times, signal, signal_err, p_orb_9, t_zero_10, timings_10, const_9, slope_9,
                                     f_n_9, a_n_9, ph_n_9, i_sectors, file_name=file_name, data_id=data_id,
                                     overwrite=overwrite, verbose=verbose)
    t_zero_11, timings_11, depths_11 = out_11
    # --- [11] --- Timing and depth error estimates
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_12.csv')
    out_12 = eclipse_analysis_timing_err(times, signal, signal_err, p_orb_9, t_zero_11, timings_11, const_9, slope_9,
                                          f_n_9, a_n_9, ph_n_9, p_err_9, noise_level_9, i_sectors, file_name=file_name,
                                          data_id=data_id, overwrite=overwrite, verbose=verbose)
    timing_errs_12, depths_12, depths_err_12 = out_12
    # --- [12] --- Determination of orbital elements
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_13.csv')
    harmonics, harmonic_n = af.find_harmonics_from_pattern(f_n_9, p_orb_9, f_tol=1e-9)
    f_h_9, a_h_9, ph_h_9 = f_n_9[harmonics], a_n_9[harmonics], ph_n_9[harmonics]
    out_13 = eclipse_analysis_elements(p_orb_9, t_zero_11, timings_11, depths_11, p_err_9,
                                       timing_errs_12, depths_err_12, f_h_9, a_h_9, ph_h_9,
                                       file_name=file_name, data_id=data_id, overwrite=overwrite, verbose=verbose)
    e_13, w_13, i_13, r_sum_sma_13, r_ratio_13, sb_ratio_13 = out_13[:6]
    # errors_13, bounds_13, formal_errors_13, dists_in_13, dists_out_13 = out_13[6:]
    # --- [13] --- Fit for the light curve parameters
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_14.csv')
    par_init_13 = (e_13, w_13, i_13, r_sum_sma_13, r_ratio_13, sb_ratio_13)
    out_14 = eclipse_analysis_fit(times, signal, signal_err, par_init_13, p_orb_9, t_zero_11, timings_11[:6], const_9,
                                  slope_9, f_n_9, a_n_9, ph_n_9, i_sectors, file_name=file_name, data_id=data_id,
                                  overwrite=overwrite, verbose=verbose)
    # par_opt_simple, par_opt_ellc = out_14
    return out_10, out_11, out_12, out_13, out_14


def pulsation_analysis_disentangle(times, signal, signal_err, ecl_model, p_orb, t_zero, const, slope, f_n, a_n, ph_n,
                                   i_sectors, file_name, data_id=None, overwrite=False, verbose=False):
    """Selects the credible frequencies from the given set,
    ignoring the harmonics

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    ecl_model: numpy.ndarray[float]
        Model of the eclipses at the same times
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    const_2: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope_2: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n_2: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n_2: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n_2: numpy.ndarray[float]
        The phases of a number of sine waves
    """
    t_a = time.time()
    n_sectors = len(i_sectors)
    fn_ext = os.path.splitext(os.path.basename(file_name))[1]
    resid = signal - ecl_model  # we extract frequencies from the residuals
    # [disentangle 1] --- initial iterative extraction of frequencies
    file_name_1 = file_name.replace(fn_ext, '_1.hdf5')
    if os.path.isfile(file_name_1) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_1, verbose=verbose)
        p_orb_1, const_1, slope_1, f_n_1, a_n_1, ph_n_1 = results
        model_1 = tsf.linear_curve(times, const_1, slope_1, i_sectors)
        model_1 += tsf.sum_sines(times, f_n_1, a_n_1, ph_n_1)
        if verbose:
            print(f'Disentangle step 1: Loaded existing results\n')
    else:
        if verbose:
            print(f'Disentangle step 1: Disentangling eclipses from other frequencies')
        t1_a = time.time()
        const_1, slope_1, f_n_1, a_n_1, ph_n_1 = tsf.extract_all(times, resid, signal_err, i_sectors, verbose=verbose)
        t1_b = time.time()
        # main function done, do the rest for this step
        model_1 = tsf.linear_curve(times, const_1, slope_1, i_sectors)
        model_1 += tsf.sum_sines(times, f_n_1, a_n_1, ph_n_1)
        n_param_1 = 2 * n_sectors + 3 * len(f_n_1)
        bic_1 = tsf.calc_bic((resid - model_1) / signal_err, n_param_1)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48mInitial frequency extraction complete.\033[0m')
            print(f'\033[0;32;48m{len(f_n_1)} frequencies, {n_param_1} free parameters. '
                  f'BIC: {bic_1:1.2f}, time taken: {t1_b - t1_a:1.1f}s\033[0m\n')
        # save
        results = (0, const_1, slope_1, f_n_1, a_n_1, ph_n_1)
        f_errors = tsf.formal_uncertainties(times, resid - model_1, a_n_1, i_sectors)
        c_err_1, sl_err_1, f_n_err_1, a_n_err_1, ph_n_err_1 = f_errors
        errors = (-1, c_err_1, sl_err_1, f_n_err_1, a_n_err_1, ph_n_err_1)
        stats = (n_param_1, bic_1, np.std(resid - model_1))
        desc = '[disentangle 1] Initial frequency extraction results.'
        ut.save_results(results, errors, stats, file_name_1, description=desc, data_id=data_id)
    # [disentangle 2] --- do a first multi-sine NL-LS fit (in chunks)
    file_name_2 = file_name.replace(fn_ext, '_2.hdf5')
    if os.path.isfile(file_name_2) & (not overwrite):
        results, errors, stats = ut.read_results(file_name_2, verbose=verbose)
        p_orb_2, const_2, slope_2, f_n_2, a_n_2, ph_n_2 = results
        p_err_2, c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2 = errors
        n_param_2, bic_2, noise_level_2 = stats
        model_2 = tsf.linear_curve(times, const_2, slope_2, i_sectors)
        model_2 += tsf.sum_sines(times, f_n_2, a_n_2, ph_n_2)
        if verbose:
            print(f'Disentangle step 2: Loaded existing results\n')
    else:
        if verbose:
            print(f'Disentangle step 2: Starting multi-sine NL-LS fit.')
        t_2a = time.time()
        f_groups = ut.group_fequencies_for_fit(a_n_1, g_min=10, g_max=15)
        out_2 = tsfit.fit_multi_sinusoid_per_group(times, resid, signal_err, const_1, slope_1, f_n_1, a_n_1, ph_n_1,
                                                   i_sectors, f_groups, verbose=verbose)
        t_2b = time.time()
        # main function done, do the rest for this step
        const_2, slope_2, f_n_2, a_n_2, ph_n_2 = out_2
        model_2 = tsf.linear_curve(times, const_2, slope_2, i_sectors)
        model_2 += tsf.sum_sines(times, f_n_2, a_n_2, ph_n_2)
        noise_level_2 = np.std(resid - model_2)
        f_errors = tsf.formal_uncertainties(times, resid - model_2, a_n_2, i_sectors)
        c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2 = f_errors
        n_param_2 = 2 * n_sectors + 3 * len(f_n_2)
        bic_2 = tsf.calc_bic((resid - model_2) / signal_err, n_param_2)
        # now print some useful info and save the result
        if verbose:
            print(f'\033[1;32;48mFit complete.\033[0m')
            print(f'\033[0;32;48m{len(f_n_2)} frequencies, {n_param_2} free parameters. '
                  f'BIC: {bic_2:1.2f}, time taken: {t_2b - t_2a:1.1f}s\033[0m\n')
        # save
        results = (0, const_2, slope_2, f_n_2, a_n_2, ph_n_2)
        errors = (-1, c_err_2, sl_err_2, f_n_err_2, a_n_err_2, ph_n_err_2)
        stats = (n_param_2, bic_2, noise_level_2)
        desc = '[disentangle 2] First multi-sine NL-LS fit results.'
        ut.save_results(results, errors, stats, file_name_2, description=desc, data_id=data_id)
        # save freqs and linear curve in ascii format at this stage
        file_name_s = file_name.replace(fn_ext, '_2_sinusoid.csv')
        data = np.column_stack((f_n_2, f_n_err_2, a_n_2, a_n_err_2, ph_n_2, ph_n_err_2))
        hdr = f'f_n_2, f_n_err_2, a_n_2, a_n_err_2, ph_n_2, ph_n_err_2'
        np.savetxt(file_name_s, data, delimiter=',', header=hdr)
        file_name_l = file_name.replace(fn_ext, '_2_linear.csv')
        data = np.column_stack((const_2, c_err_2, slope_2, sl_err_2, i_sectors[:, 0], i_sectors[:, 1]))
        hdr = (f'const_2, c_err_2, slope_2, sl_err_2, sector_start, sector_end')
        np.savetxt(file_name_l, data, delimiter=',', header=hdr)
    if (len(f_n_2) == 0):
        if verbose:
            print(f'No frequencies found.')
        np.savetxt(file_name.replace(fn_ext, '.txt'), ['No frequencies found'], fmt='%s')
    # full model
    model_full = model_2 + ecl_model
    n_param = n_param_2 + 7  # sinusoids, 6 for eclipse model and 1 for period
    resid_full = signal - model_full
    bic_full = tsf.calc_bic(resid_full/signal_err, n_param)
    t_b = time.time()
    if verbose:
        print(f'\033[1;32;48mSinusoid model disentangled.\033[0m')
        print(f'\033[0;32;48mNumber of frequencies: {len(f_n_2)}, \n'
              f'BIC of eclipse model plus sinusoids: {bic_full:1.2f}. \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return const_2, slope_2, f_n_2, a_n_2, ph_n_2


def pulsation_analysis_full_fit(times, signal, signal_err, p_orb, t_zero, ecl_par, const, slope, f_n, a_n, ph_n,
                                i_sectors, model, file_name, data_id=None, overwrite=False, verbose=False):
    """Selects the credible frequencies from the given set,
    ignoring the harmonics

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    p_orb: float
        Orbital period of the eclipsing binary in days
    t_zero: float
        Time of deepest minimum modulo p_orb
    ecl_par: numpy.ndarray[float]
        Initial eclipse parameters to start the fit, consisting of:
        e, w, i, r_sum_sma, r_ratio, sb_ratio
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    model: str
        Which eclipse light curve model to use. Choose 'simple' or 'ellc'
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    p_orb: float
        Orbital period of the eclipsing binary
    t_zero: float
        Time of deepest minimum
    ecl_par: numpy.ndarray[float]
        Eclipse parameters, consisting of:
        e, w, i, r_sum_sma, r_ratio, sb_ratio
    const: numpy.ndarray[float]
        The y-intercept(s) of a piece-wise linear curve
    slope: numpy.ndarray[float]
        The slope(s) of a piece-wise linear curve
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    """
    t_a = time.time()
    n_sectors = len(i_sectors)
    fn_ext = os.path.splitext(os.path.basename(file_name))[1]
    # multi-sine NL-LS fit with eclipse model (in chunks)
    file_name_1 = file_name.replace(fn_ext, '.hdf5')
    file_name_2 = file_name.replace(fn_ext, '_eclipse_par.csv')
    if os.path.isfile(file_name_1) & os.path.isfile(file_name_2) & (not overwrite):
        full_results = ut.read_results_ecl_sin_lin(file_name_1, verbose=verbose)
        results, errors, stats, p_orb, t_zero, e, w, i, r_sum_sma, r_ratio, sb_ratio = full_results
        ecl_par = e, w, i, r_sum_sma, r_ratio, sb_ratio
        _, const, slope, f_n, a_n, ph_n = results
        p_err, c_err, sl_err, f_n_err, a_n_err, ph_n_err = errors
        n_param, bic, noise_level = stats
        if verbose:
            print(f'Loaded existing full fit results\n')
    else:
        if verbose:
            print(f'Starting multi-sine NL-LS fit with {model} eclipse model.')
        f_groups = ut.group_fequencies_for_fit(a_n, g_min=10, g_max=15)
        out = tsfit.fit_multi_sinusoid_eclipse_per_group(times, signal, signal_err, p_orb, t_zero, ecl_par, const,
                                                         slope, f_n, a_n, ph_n, i_sectors, f_groups, verbose=verbose)
        # main function done, do the rest for this step
        p_orb, t_zero, ecl_par, const, slope, f_n, a_n, ph_n = out
        ecosw, esinw, i, r_sum_sma, r_ratio, sb_ratio = ecl_par
        e, w = np.sqrt(ecosw**2 + esinw**2), np.arctan2(esinw, ecosw) % (2 * np.pi)
        # make model including everything to calculate BIC and noise level
        model_full = tsf.linear_curve(times, const, slope, i_sectors)
        model_full += tsf.sum_sines(times, f_n, a_n, ph_n)
        if (model == 'ellc'):
            f_c, f_s = e**0.5 * np.cos(w), e**0.5 * np.sin(w)
            model_full += tsfit.wrap_ellc_lc(times, p_orb, t_zero, f_c, f_s, i, r_sum_sma, r_ratio, sb_ratio, 0)
        else:
            model_full += tsfit.simple_eclipse_lc(times, p_orb, t_zero, e, w, i, r_sum_sma, r_ratio, sb_ratio)
        residuals = signal - model_full
        noise_level = np.std(residuals)
        f_errors = tsf.formal_uncertainties(times, residuals, a_n, i_sectors)
        c_err, sl_err, f_n_err, a_n_err, ph_n_err = f_errors
        n_param = 2 + len(ecl_par) + 2 * n_sectors + 3 * len(f_n)
        bic = tsf.calc_bic(residuals / signal_err, n_param)
        # save everything
        results = (p_orb, const, slope, f_n, a_n, ph_n)
        errors = (-1, c_err, sl_err, f_n_err, a_n_err, ph_n_err)
        stats = (n_param, bic, noise_level)
        desc = '[16] Results of multi-sine NL-LS fit with eclipse model.'
        ut.save_results_ecl_sin_lin(results, errors, stats, p_orb, t_zero, e, w, i, r_sum_sma, r_ratio, sb_ratio,
                                    i_sectors, file_name_1, description=desc, data_id=data_id)
    t_b = time.time()
    if verbose:
        print(f'\033[1;32;48mFit complete.\033[0m')
        print(f'\033[0;32;48mNumber of frequencies: {len(f_n)}, \n'
              f'BIC of eclipse model plus sinusoids: {bic:1.2f}. \n'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return p_orb, t_zero, ecl_par, const, slope, f_n, a_n, ph_n


def pulsation_analysis_fselect(times, signal, ecl_model, f_n, a_n, ph_n, noise_level, i_sectors, file_name,
                               data_id=None, overwrite=False, verbose=False):
    """Selects the credible frequencies from the given set,
    ignoring the harmonics

    Parameters
    ----------
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    ecl_model: numpy.ndarray[float]
        Model of the eclipses at the same times
    f_n: numpy.ndarray[float]
        The frequencies of a number of sine waves
    a_n: numpy.ndarray[float]
        The amplitudes of a number of sine waves
    ph_n: numpy.ndarray[float]
        The phases of a number of sine waves
    noise_level: float
        The noise level (standard deviation of the residuals)
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    file_name: str
        File name (including path) for saving the results. Also used to
        load previous analysis results if found.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    passed_nh_sigma: numpy.ndarray[bool]
        Non-harmonic frequencies that passed the sigma check
    passed_nh_snr: numpy.ndarray[bool]
        Non-harmonic frequencies that passed the signal-to-noise check
    passed_nh_b: numpy.ndarray[bool]
        Non-harmonic frequencies that passed both checks
    """
    t_a = time.time()
    if os.path.isfile(file_name) & (not overwrite):
        if verbose:
            print(f'Loading existing results {os.path.splitext(os.path.basename(file_name))[0]}')
        passed_nh_sigma, passed_nh_snr, passed_nh_b = ut.read_results_fselect(file_name)
    else:
        if verbose:
            print(f'Selecting credible frequencies')
        n_points = len(times)
        # obtain the errors on the sine waves (residual and thus model dependent)
        errors = tsf.formal_uncertainties(times, signal - ecl_model, a_n, i_sectors)
        const_err, slope_err, f_n_err, a_n_err, ph_n_err = errors
        # find the insignificant frequencies
        remove_sigma = af.remove_insignificant_sigma(f_n, f_n_err, a_n, a_n_err, sigma_a=3., sigma_f=1.)
        remove_snr = af.remove_insignificant_snr(a_n, noise_level, n_points)
        # frequencies that pass sigma criteria
        passed_nh_sigma = np.ones(len(f_n), dtype=bool)
        passed_nh_sigma[remove_sigma] = False
        # frequencies that pass S/N criteria
        passed_nh_snr = np.ones(len(f_n), dtype=bool)
        passed_nh_snr[remove_snr] = False
        # passing both
        passed_nh_b = (passed_nh_sigma & passed_nh_snr)
        # save
        ut.save_results_fselect(f_n, a_n, ph_n, passed_nh_sigma, passed_nh_snr, file_name, data_id)
    t_b = time.time()
    if verbose:
        print(f'\033[1;32;48mNon-harmonic frequencies selected.\033[0m')
        print(f'\033[0;32;48mNumber of frequencies passed: {len(passed_nh_b)} of {len(f_n)}.'
              f'Time taken: {t_b - t_a:1.1f}s\033[0m\n')
    return passed_nh_sigma, passed_nh_snr, passed_nh_b


def pulsation_analysis(tic, times, signal, signal_err, i_sectors, save_dir, data_id=None, overwrite=False,
                       verbose=False):
    """Part three of analysis recipe for analysis of EB light curves,
    to be chained after frequency_analysis and eclipse_analysis

    Parameters
    ----------
    tic: int
        The TESS Input Catalog number for later reference
        Use any number (or even str) as reference if not available.
    times: numpy.ndarray[float]
        Timestamps of the time-series
    signal: numpy.ndarray[float]
        Measurement values of the time-series
    signal_err: numpy.ndarray[float]
        Errors in the measurement values
    i_sectors: list[int], numpy.ndarray[int]
        Pair(s) of indices indicating the separately handled timespans
        in the piecewise-linear curve. These can indicate the TESS
        observation sectors, but taking half the sectors is recommended.
        If only a single curve is wanted, set
        i_half_s = np.array([[0, len(times)]]).
    save_dir: str
        Path to a directory for saving the results. Also used to load
        previous analysis results.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    verbose: bool
        If set to True, this function will print some information

    Returns
    -------
    out_15: tuple
        output of pulsation_analysis_disentangle
    out_16: tuple
        output of pulsation_analysis_full_fit
    out_16b: tuple
        output of pulsation_analysis_full_fit for ellc model
    out_17: tuple
        output of pulsation_analysis_fselect
    out_17b: tuple
        output of pulsation_analysis_fselect for ellc model
    """
    signal_err = np.max(signal_err) * np.ones(len(times))  # likelihood assumes the same errors
    # read in the frequency analysis results
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_9.hdf5')
    if os.path.isfile(file_name):
        results, errors, stats = ut.read_results(file_name, verbose=verbose)
    else:
        if verbose:
            print('No prewhitening results found')
        return (None,) * 4
    p_orb_9, const_9, slope_9, f_n_9, a_n_9, ph_n_9 = results
    p_orb_9 = p_orb_9[0]  # must be a float
    p_err_9, c_err_9, sl_err_9, f_n_err_9, a_n_err_9, ph_n_err_9 = errors
    n_param_9, bic_9, noise_level_9 = stats
    # load t_zero from the timings file
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_11.csv')
    if os.path.isfile(file_name):
        t_zero_11, timings_11, depths_11 = ut.read_results_cubics(file_name)
    else:
        if verbose:
            print('No timing results found')
        return (None,) * 4
    # open the orbital elements file
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_14.csv')
    if os.path.isfile(file_name):
        par_init_13, par_opt1_14, par_opt2_14 = ut.read_results_lc_fit(file_name)
    else:
        if verbose:
            print('No elements results found')
        return (None,) * 4
    # make the eclipse models
    ecl_model_simple = tsfit.simple_eclipse_lc(times, p_orb_9, t_zero_11, *par_opt1_14)
    ecl_model_ellc = tsfit.wrap_ellc_lc(times, p_orb_9, t_zero_11, *par_opt2_14, 0)
    # --- [15] --- Eclipse model disentangling
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_15.csv')
    out_15 = pulsation_analysis_disentangle(times, signal, signal_err, ecl_model_simple, p_orb_9, t_zero_11,
                                            const_9, slope_9, f_n_9, a_n_9, ph_n_9, i_sectors, file_name=file_name,
                                            data_id=data_id, overwrite=overwrite, verbose=verbose)
    const_r1, slope_r1, f_n_r1, a_n_r1, ph_n_r1 = out_15
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_15_ellc.csv')
    out_15b = pulsation_analysis_disentangle(times, signal, signal_err, ecl_model_ellc, p_orb_9, t_zero_11,
                                             const_9, slope_9, f_n_9, a_n_9, ph_n_9, i_sectors, file_name=file_name,
                                             data_id=data_id, overwrite=overwrite, verbose=verbose)
    const_r2, slope_r2, f_n_r2, a_n_r2, ph_n_r2 = out_15b
    # --- [16] --- Full model fit
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_16.csv')
    out_16 = pulsation_analysis_full_fit(times, signal, signal_err, p_orb_9, t_zero_11, par_opt2_14,
                                         const_9, slope_9, f_n_9, a_n_9, ph_n_9, i_sectors, model='simple',
                                         file_name=file_name, data_id=data_id, overwrite=overwrite, verbose=verbose)
    p_orb_r1, t_zero_r1, ecl_par_r1, const_r1, slope_r1, f_n_r1, a_n_r1, ph_n_r1 = out_16
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_16_ellc.csv')
    out_16b = pulsation_analysis_full_fit(times, signal, signal_err, p_orb_9, t_zero_11, par_opt2_14,
                                          const_9, slope_9, f_n_9, a_n_9, ph_n_9, i_sectors, model='ellc',
                                          file_name=file_name, data_id=data_id, overwrite=overwrite, verbose=verbose)
    p_orb_r2, t_zero_r2, ecl_par_r2, const_r2, slope_r2, f_n_r2, a_n_r2, ph_n_r2 = out_16b
    # --- [17] --- Frequency selection
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_17.csv')
    out_17 = pulsation_analysis_fselect(times, signal, ecl_model_simple, f_n_r1, a_n_r1, ph_n_r1, noise_level_9,
                                        i_sectors, file_name=file_name, data_id=data_id, overwrite=overwrite,
                                        verbose=verbose)
    # pass_nh_sigma, pass_nh_snr, passed_nh_b = out_17
    file_name = os.path.join(save_dir, f'tic_{tic}_analysis', f'tic_{tic}_analysis_17_ellc.csv')
    out_17b = pulsation_analysis_fselect(times, signal, ecl_model_ellc, f_n_r2, a_n_r2, ph_n_r2, noise_level_9,
                                         i_sectors, file_name=file_name, data_id=data_id, overwrite=overwrite,
                                         verbose=verbose)
    # pass_nh_sigma, pass_nh_snr, passed_nh_b = out_17b
    # --- [18] --- Harmonics in the residuals
    # determine which residual frequencies are consistent with harmonics
    # --- [19] --- Amplitude modulation
    # use wavelet transform or smth to see which star is pulsating
    return out_15, out_16, out_16b, out_17, out_17b


def analyse_from_file(file_name, p_orb=0, data_id=None, overwrite=False, verbose=False):
    """Do all steps of the analysis for a given light curve file

    Parameters
    ----------
    file_name: str
        Path to a file containing the light curve data, with
        timestamps, normalised flux, error values as the
        first three columns, respectively.
    p_orb: float
        Orbital period of the eclipsing binary in days
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    
    Returns
    -------
    None
    
    Notes
    -----
    Results are saved in the same directory as the given file
    """
    # load the data
    target_id = os.path.splitext(os.path.basename(file_name))[0]  # file name is used as target identifier
    save_dir = os.path.dirname(file_name)
    times, signal, signal_err = np.loadtxt(file_name, usecols=(0, 1, 2), unpack=True)
    times = times - times[0]  # translate time array to start at zero
    i_half_s = np.array([[0, len(times)]])  # no sector information
    kw_args = {'save_dir': save_dir, 'data_id': data_id, 'overwrite': overwrite, 'verbose': verbose}
    # do the analysis
    out_a = frequency_analysis(target_id, times, signal, signal_err, i_half_s, p_orb=0, **kw_args)
    # if not full output, stop
    if not (len(out_a[0]) < 8):
        out_b = eclipse_analysis(target_id, times, signal, signal_err, i_half_s, **kw_args)
        if not np.all([item is None for item in out_b]):
            out_c = pulsation_analysis(target_id, times, signal, signal_err, i_half_s, **kw_args)
    if verbose:
        print('done.')
    return None


def analyse_from_tic(tic, all_files, p_orb=0, save_dir=None, data_id=None, overwrite=False, verbose=False):
    """Do all steps of the analysis for a given TIC number
    
    Parameters
    ----------
    tic: int
        The TESS Input Catalog (TIC) number for loading the data
        and later reference.
    all_files: list[str]
        List of all the TESS data product '.fits' files. The files
        with the corresponding TIC number are selected.
    p_orb: float
        Orbital period of the eclipsing binary in days
    save_dir: str
        Path to a directory for saving the results. Also used to load
        previous analysis results.
    data_id: int, str, None
        Identification for the dataset used
    overwrite: bool
        If set to True, overwrite old results in the same directory as
        save_dir, or (if False) to continue from the last save-point.
    
    Returns
    -------
    None
    """
    # load the data
    lc_data = ut.load_tess_lc(tic, all_files, apply_flags=True)
    times, sap_signal, signal, signal_err, sectors, t_sectors, crowdsap = lc_data
    i_sectors = ut.convert_tess_t_sectors(times, t_sectors)
    lc_processed = ut.stitch_tess_sectors(times, signal, signal_err, i_sectors)
    times, signal, signal_err, sector_medians, times_0, t_combined, i_half_s = lc_processed
    kw_args = {'save_dir': save_dir, 'data_id': data_id, 'overwrite': overwrite, 'verbose': verbose}
    # do the analysis
    out_a = frequency_analysis(tic, times, signal, signal_err, i_half_s, p_orb=0, **kw_args)
    # if not full output, stop
    if not (len(out_a[0]) < 8):
        out_b = eclipse_analysis(tic, times, signal, signal_err, i_half_s, **kw_args)
        if not np.all([item is None for item in out_b]):
            out_c = pulsation_analysis(tic, times, signal, signal_err, i_half_s, **kw_args)
    if verbose:
        print('done.')
    return None


def analyse_set(target_list, function='analyse_from_tic', n_threads=os.cpu_count() - 2, **kwargs):
    """Analyse a set of light curves in parallel
    
    Parameters
    ----------
    target_list: list[str], list[int]
        List of either file names or TIC identifiers to analyse
    function: str
        Name  of the function to use for the analysis
        Choose from [analyse_from_file, analyse_from_tic]
    n_threads: int
        Number of threads to use. Uses two less than the
        available amount by default.
    **kwargs: dict
        Extra arguments to 'function': refer to each function's
        documentation for a list of all possible arguments.
    
    Returns
    -------
    None
    """
    if 'p_orb' in kwargs.keys():
        # Use mp.Pool.starmap for this
        raise NotImplementedError('keyword p_orb found in kwargs: this functionality is not yet implemented')
    t1 = time.time()
    with mp.Pool(processes=n_threads) as pool:
        pool.map(fct.partial(eval(function), **kwargs), target_list, chunksize=1)
    t2 = time.time()
    print(f'Finished analysing set in: {(t2 - t1):1.2} s ({(t2 - t1) / 3600:1.2} h) for {len(target_list)} targets,\n'
          f'using {n_threads} threads ({(t2 - t1) * n_threads / len(target_list):1.2} s '
          f'average per target single threaded).')
    return None
