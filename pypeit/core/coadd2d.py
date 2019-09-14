"""
Module for performing two-dimensional coaddition of spectra.

.. _numpy.ndarray: https://docs.scipy.org/doc/numpy/reference/generated/numpy.ndarray.html

"""
import os

import numpy as np
import scipy
import copy

from astropy.io import fits
from astropy import stats

from pypeit import msgs
from pypeit import utils
from pypeit.masterframe import MasterFrame
from pypeit.waveimage import WaveImage
from pypeit.wavetilts import WaveTilts
from pypeit.traceslits import TraceSlits
from pypeit.images import scienceimage
from pypeit import reduce
from pypeit.core import extract

from pypeit.core import load, coadd1d, pixels
from pypeit.core import parse
from pypeit.spectrographs import util
from matplotlib import pyplot as plt
from IPython import embed
from pypeit import ginga
from pypeit import specobjs


# TODO make weights optional and do uniform weighting without.
def weighted_combine(weights, sci_list, var_list, inmask_stack,
                     sigma_clip=False, sigma_clip_stack = None, sigrej=None, maxiters=5):
    """

    Args:
        weights: float ndarray of weights.
            Options for the shape of weights are:
                (nimgs,)              -- a single weight per image in the stack
                (nimgs, nspec)        -- wavelength dependent weights per image in the stack
                (nimgs, nspec, nspat) -- weights input with the shape of the image stack

             Note that the weights are distinct from the mask which is dealt with via inmask_stack argument so there
             should not be any weights that are set to zero (although in principle this would still work).

        sci_list: list
            List of  float ndarray images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be combined with the  weights, inmask_stack, and possibly sigma clipping
        var_list: list
            List of  float ndarray variance images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be combined with proper erorr propagation, i.e.
            using the  weights**2, inmask_stack, and possibly sigma clipping
        inmask_stack: ndarray, boolean, shape (nimgs, nspec, nspat)
            Array of input masks for the images. True = Good, False=Bad
        sigma_clip: bool, default = False
            Combine with a mask by sigma clipping the image stack. Only valid if nimgs > 2
        sigma_clip_stack: ndarray, float, shape (nimgs, nspec, nspat), default = None
            The image stack to be used for the sigma clipping. For example if
            if the list of images to be combined with the weights is [sciimg_stack, waveimg_stack, tilts_stack] you
            would be sigma clipping with sciimg_stack, and would set sigma_clip_stack = sciimg_stack
        sigrej: int or float, default = None
            Rejection threshold for sigma clipping. Code defaults to determining this automatically based
            on the numberr of images provided.
        maxiters:
            Maximum number of iterations for sigma clipping using astropy.stats.SigmaClip

    Returns:
        sci_list_out: list
           The list of ndarray float combined images with shape (nspec, nspat)
        var_list_out: list
           The list of ndarray propagated variance images with shape (nspec, nspat)
        outmask: bool ndarray, shape (nspec, nspat)
           Mask for combined image. True=Good, False=Bad
        nused: int ndarray, shape (nspec, nspat)
           Image of integers indicating the number of images that contributed to each pixel
    """

    shape = img_list_error_check(sci_list, var_list)

    nimgs = shape[0]
    img_shape = shape[1:]
    #nspec = shape[1]
    #nspat = shape[2]

    if nimgs == 1:
        # If only one image is passed in, simply return the input lists of images, but reshaped
        # to be (nspec, nspat)
        msgs.warn('Cannot combine a single image. Returning input images')
        sci_list_out = []
        for sci_stack in sci_list:
            sci_list_out.append(sci_stack.reshape(img_shape))
        var_list_out = []
        for var_stack in var_list:
            var_list_out.append(var_stack.reshape(img_shape))
        outmask = inmask_stack.reshape(img_shape)
        nused = outmask.astype(int)
        return sci_list_out, var_list_out, outmask, nused

    if sigma_clip and nimgs >= 3:
        if sigma_clip_stack is None:
            msgs.error('You must specify sigma_clip_stack, i.e. which quantity to use for sigma clipping')
        if sigrej is None:
            if nimgs <= 2:
                sigrej = 100.0  # Irrelevant for only 1 or 2 files, we don't sigma clip below
            elif nimgs == 3:
                sigrej = 1.1
            elif nimgs == 4:
                sigrej = 1.3
            elif nimgs == 5:
                sigrej = 1.6
            elif nimgs == 6:
                sigrej = 1.9
            else:
                sigrej = 2.0
        # sigma clip if we have enough images
        # mask_stack > 0 is a masked value. numpy masked arrays are True for masked (bad) values
        data = np.ma.MaskedArray(sigma_clip_stack, np.invert(inmask_stack))
        sigclip = stats.SigmaClip(sigma=sigrej, maxiters=maxiters, cenfunc='median', stdfunc=utils.nan_mad_std)
        data_clipped, lower, upper = sigclip(data, axis=0, masked=True, return_bounds=True)
        mask_stack = np.invert(data_clipped.mask)  # mask_stack = True are good values
    else:
        if sigma_clip and nimgs < 3:
            msgs.warn('Sigma clipping requested, but you cannot sigma clip with less than 3 images. '
                      'Proceeding without sigma clipping')
        mask_stack = inmask_stack  # mask_stack = True are good values

    nused = np.sum(mask_stack, axis=0)
    weights_stack = broadcast_weights(weights, shape)
    weights_mask_stack = weights_stack*mask_stack

    weights_sum = np.sum(weights_mask_stack, axis=0)
    sci_list_out = []
    for sci_stack in sci_list:
        sci_list_out.append(np.sum(sci_stack*weights_mask_stack, axis=0)/(weights_sum + (weights_sum == 0.0)))
    var_list_out = []
    for var_stack in var_list:
        var_list_out.append(np.sum(var_stack * weights_mask_stack**2, axis=0) / (weights_sum + (weights_sum == 0.0))**2)
    # Was it masked everywhere?
    outmask = np.any(mask_stack, axis=0)

    return sci_list_out, var_list_out, outmask, nused


def reference_trace_stack(slitid, stack_dict, offsets=None, objid=None):

    if offsets is not None and objid is not None:
        msgs.errror('You can only input offsets or an objid, but not both')
    nexp = len(offsets) if offsets is not None else len(objid)
    # There are two modes of operation to determine the reference trace for the 2d coadd of a given slit/order
    # --------------------------------------------------------------------------------------------------------
    # 1) offsets: we stack about the central trace for the slit in question with the input offsets added
    # 2) ojbid: we stack about the trace of reference object for this slit given for each exposure by the input objid
    if offsets is not None:
        tslits_dict_list = stack_dict['tslits_dict_list']
        nspec, nslits = tslits_dict_list[0]['slit_left'].shape
        ref_trace_stack = np.zeros((nspec, nexp))
        for iexp, tslits_dict in enumerate(tslits_dict_list):
            ref_trace_stack[:, iexp] = (tslits_dict[:, slitid]['slit_left'] + tslits_dict[:, slitid]['slit_righ'])/2.0 + offsets[iexp]
    elif objid is not None:
        specobjs_list = stack_dict['specobjs_list']
        nspec = specobjs_list[0][0].trace_spat.shape[0]
        # Grab the traces, flux, wavelength and noise for this slit and objid.
        ref_trace_stack = np.zeros((nspec, nexp), dtype=float)
        for iexp, sobjs in enumerate(specobjs_list):
            ithis = (sobjs.slitid == slitid) & (sobjs.objid == objid[iexp])
            ref_trace_stack[:, iexp] = sobjs[ithis].trace_spat
    else:
        msgs.error('You must input either offsets or an objid to determine the stack of reference traces')

    return ref_trace_stack

def optimal_weights(specobjs_list, slitid, objid, sn_smooth_npix, const_weights=False):
    """
    Determine optimal weights for 2d coadds. This script grabs the information from SpecObjs list for the
    object with specified slitid and objid and passes to coadd.sn_weights to determine the optimal weights for
    each exposure. This routine will also pass back the trace and the wavelengths (optimally extracted) for each
    exposure.

    Args:
        specobjs_list: list
           list of SpecObjs objects contaning the objects that were extracted from each frame that will contribute
           to the coadd.
        slitid: int
           The slitid that has the brightest object whose S/N will be used to determine the weight for each frame.
        objid: int
           The objid index of the brightest object whose S/N will be used to determine the weight for each frame.

    Returns:
        (rms_sn, weights)
        rms_sn : ndarray, shape = (len(specobjs_list),)
            Root mean square S/N value for each input spectra
        weights : ndarray, shape (len(specobjs_list),)
            Weights to be applied to the spectra. These are signal-to-noise squared weights.
    """

    nexp = len(specobjs_list)
    nspec = specobjs_list[0][0].trace_spat.shape[0]
    # Grab the traces, flux, wavelength and noise for this slit and objid.
    flux_stack = np.zeros((nspec, nexp), dtype=float)
    ivar_stack = np.zeros((nspec, nexp), dtype=float)
    wave_stack = np.zeros((nspec, nexp), dtype=float)
    mask_stack = np.zeros((nspec, nexp), dtype=bool)

    for iexp, sobjs in enumerate(specobjs_list):
        ithis = (sobjs.slitid == slitid) & (sobjs.objid == objid[iexp])
        flux_stack[:,iexp] = sobjs[ithis][0].optimal['COUNTS']
        ivar_stack[:,iexp] = sobjs[ithis][0].optimal['COUNTS_IVAR']
        wave_stack[:,iexp] = sobjs[ithis][0].optimal['WAVE']
        mask_stack[:,iexp] = sobjs[ithis][0].optimal['MASK']

    # TODO For now just use the zero as the reference for the wavelengths? Perhaps we should be rebinning the data though?
    rms_sn, weights = coadd1d.sn_weights(wave_stack, flux_stack, ivar_stack, mask_stack, sn_smooth_npix,
                                         const_weights=const_weights)
    return rms_sn, weights.T

def det_error_msg(exten, sdet):
    # Print out error message if extension is not found
    msgs.error("Extension {:s} for requested detector {:s} was not found.\n".format(exten)  +
               " Maybe you chose the wrong detector to coadd? "
               "Set with --det= or check file contents with pypeit_show_2dspec Science/spec2d_XXX --list".format(sdet))


def get_wave_ind(wave_grid, wave_min, wave_max):
    """
    Utility routine used by coadd2d to determine the starting and ending indices of a wavelength grid.

    Args:
        wave_grid: float ndarray
          Wavelength grid.
        wave_min: float
          Minimum wavelength covered by the data in question.
        wave_max: float
          Maximum wavelength covered by the data in question.

    Returns:
        (ind_lower, ind_upper): tuple, int
          Integer lower and upper indices into the array wave_grid that cover the interval (wave_min, wave_max)
    """

    diff = wave_grid - wave_min
    diff[diff > 0] = np.inf
    if not np.any(diff < 0):
        ind_lower = 0
        msgs.warn('Your wave grid does not extend blue enough. Taking bluest point')
    else:
        ind_lower = np.argmin(np.abs(diff))
    diff = wave_max - wave_grid
    diff[diff > 0] = np.inf
    if not np.any(diff < 0):
        ind_upper = wave_grid.size-1
        msgs.warn('Your wave grid does not extend red enough. Taking reddest point')
    else:
        ind_upper = np.argmin(np.abs(diff))

    return ind_lower, ind_upper

def broadcast_weights(weights, shape):
    """
    Utility routine to broadcast weights to be the size of image stacks specified by shape
    Args:
        weights: float ndarray of weights.
            Options for the shape of weights are:
                (nimgs,)              -- a single weight per image
                (nimgs, nspec)        -- wavelength dependent weights per image
                (nimgs, nspec, nspat) -- weights already have the shape of the image stack and are simply returned
        shape: tuple of integers
            Shape of the image stacks for weighted coadding. This is either (nimgs, nspec) for 1d extracted spectra or
            (nimgs, nspec, nspat) for 2d spectrum images

    Returns:

    """
    # Create the weights stack images from the wavelength dependent weights, i.e. propagate these
    # weights to the spatial direction
    if weights.ndim == 1:
        # One float per image
        if len(shape) == 2:
            weights_stack = np.einsum('i,ij->ij', weights, np.ones(shape))
        elif len(shape) == 3:
            weights_stack = np.einsum('i,ijk->ijk', weights, np.ones(shape))
        else:
            msgs.error('Image shape is not supported')
    elif weights.ndim == 2:
        # Wavelength dependent weights per image
        if len(shape) == 2:
            if weights.shape != shape:
                msgs.error('The shape of weights does not match the shape of the image stack')
            weights_stack = weights
        elif len(shape) == 3:
            weights_stack = np.einsum('ij,k->ijk', weights, np.ones(shape[2]))
    elif weights.ndim == 3:
        # Full image stack of weights
        if weights.shape != shape:
            msgs.error('The shape of weights does not match the shape of the image stack')
        weights_stack = weights
    else:
        msgs.error('Unrecognized dimensionality for weights')

    return weights_stack

def get_wave_bins(thismask_stack, waveimg_stack, wave_grid):

    # Determine the wavelength grid that we will use for the current slit/order
    # TODO This cut on waveimg_stack should not be necessary
    wavemask = thismask_stack & (waveimg_stack > 1.0)
    wave_lower = waveimg_stack[wavemask].min()
    wave_upper = waveimg_stack[wavemask].max()
    ind_lower, ind_upper = get_wave_ind(wave_grid, wave_lower, wave_upper)
    wave_bins = wave_grid[ind_lower:ind_upper + 1]

    return wave_bins


def get_spat_bins(thismask_stack, trace_stack):

    nimgs, nspec, nspat = thismask_stack.shape
    # Create the slit_cen_stack and determine the minimum and maximum
    # spatial offsets that we need to cover to determine the spatial
    # bins
    spat_img = np.outer(np.ones(nspec), np.arange(nspat))
    dspat_stack = np.zeros_like(thismask_stack,dtype=float)
    spat_min = np.inf
    spat_max = -np.inf
    for img in range(nimgs):
        # center of the slit replicated spatially
        slit_cen_img = np.outer(trace_stack[:, img], np.ones(nspat))
        dspat_iexp = (spat_img - slit_cen_img)
        dspat_stack[img, :, :] = dspat_iexp
        thismask_now = thismask_stack[img, :, :]
        spat_min = np.fmin(spat_min, dspat_iexp[thismask_now].min())
        spat_max = np.fmax(spat_max, dspat_iexp[thismask_now].max())

    spat_min_int = int(np.floor(spat_min))
    spat_max_int = int(np.ceil(spat_max))
    dspat_bins = np.arange(spat_min_int, spat_max_int + 1, 1,dtype=float)

    return dspat_bins, dspat_stack


def compute_coadd2d(ref_trace_stack, sciimg_stack, sciivar_stack, skymodel_stack, inmask_stack, tilts_stack,
                    thismask_stack, waveimg_stack, wave_grid, weights='uniform'):
    """
    Construct a 2d co-add of a stack of PypeIt spec2d reduction outputs.

    Slits are 'rectified' onto a spatial and spectral grid, which
    encompasses the spectral and spatial coverage of the image stacks.
    The rectification uses nearest grid point interpolation to avoid
    covariant errors.  Dithering is supported as all images are centered
    relative to a set of reference traces in trace_stack.

    Args:
        trace_stack (`numpy.ndarray`_):
            Stack of reference traces about which the images are
            rectified and coadded.  If the images were not dithered then
            this reference trace can simply be the center of the slit::

                slitcen = (slit_left + slit_righ)/2

            If the images were dithered, then this object can either be
            the slitcen appropriately shifted with the dither pattern,
            or it could be the trace of the object of interest in each
            exposure determined by running PypeIt on the individual
            images.  Shape is (nimgs, nspec).
        sciimg_stack (`numpy.ndarray`_):
            Stack of science images.  Shape is (nimgs, nspec, nspat).
        sciivar_stack (`numpy.ndarray`_):
            Stack of inverse variance images.  Shape is (nimgs, nspec,
            nspat).
        skymodel_stack (`numpy.ndarray`_):
            Stack of the model sky.  Shape is (nimgs, nspec, nspat).
        inmask_stack (`numpy.ndarray`_):
            Boolean array with the input masks for each image; `True`
            values are *good*, `False` values are *bad*.  Shape is
            (nimgs, nspec, nspat).
        tilts_stack (`numpy.ndarray`_):
           Stack of the wavelength tilts traces.  Shape is (nimgs,
           nspec, nspat).
        waveimg_stack (`numpy.ndarray`_):
           Stack of the wavelength images.  Shape is (nimgs, nspec,
           nspat).
        thismask_stack (`numpy.ndarray`_):
            Boolean array with the masks indicating which pixels are on
            the slit in question.  `True` values are on the slit;
            `False` values are off the slit.  Shape is (nimgs, nspec,
            nspat).
        weights (`numpy.ndarray`_, optional):
            The weights used when combining the rectified images (see
            :func:`weighted_combine`).  If no weights are provided,
            uniform weighting is used.  Weights are broadast to the
            correct size of the image stacks (see
            :func:`broadcast_weights`), as necessary.  Shape must be
            (nimgs,), (nimgs, nspec), or (nimgs, nspec, nspat).
        loglam_grid (`numpy.ndarray`_, optional):
            Wavelength grid in log10(wave) onto which the image stacks
            will be rectified.  The code will automatically choose the
            subset of this grid encompassing the wavelength coverage of
            the image stacks provided (see :func:`waveimg_stack`).
            Either `loglam_grid` or `wave_grid` must be provided.
        wave_grid (`numpy.ndarray`_, optional):
            Same as `loglam_grid` but in angstroms instead of
            log(angstroms). (TODO: Check units...)

    Returns:
        TODO: This needs to be updated.

        (sciimg, sciivar, imgminsky, outmask, nused, tilts, waveimg, dspat, thismask, tslits_dict)

        sciimg: float ndarray shape = (nspec_coadd, nspat_coadd)
            Rectified and coadded science image
        sciivar: float ndarray shape = (nspec_coadd, nspat_coadd)
            Rectified and coadded inverse variance image with correct error propagation
        imgminsky: float ndarray shape = (nspec_coadd, nspat_coadd)
            Rectified and coadded sky subtracted image
        outmask: bool ndarray shape = (nspec_coadd, nspat_coadd)
            Output mask for rectified and coadded images. True = Good, False=Bad.
        nused: int ndarray shape = (nspec_coadd, nspat_coadd)
            Image of integers indicating the number of images from the image stack that contributed to each pixel
        tilts: float ndarray shape = (nspec_coadd, nspat_coadd)
            The averaged tilts image corresponding to the rectified and coadded data.
        waveimg: float ndarray shape = (nspec_coadd, nspat_coadd)
            The averaged wavelength image corresponding to the rectified and coadded data.
        dspat: float ndarray shape = (nspec_coadd, nspat_coadd)
            The average spatial offsets in pixels from the reference trace trace_stack corresponding to the rectified
            and coadded data.
        thismask: bool ndarray shape = (nspec_coadd, nspat_coadd)
            Output mask for rectified and coadded images. True = Good, False=Bad. This image is trivial, and
            is simply an image of True values the same shape as the rectified and coadded data.
        tslits_dict: dict
            tslits_dict dictionary containing the information about the slits boundaries. The slit boundaries
            are trivial and are simply vertical traces at 0 and nspat_coadd-1.
    """
    nimgs, nspec, nspat = sciimg_stack.shape

    if 'uniform' in weights:
        msgs.info('No weights were provided. Using uniform weights.')
        weights = np.ones(nimgs)/float(nimgs)

    weights_stack = broadcast_weights(weights, sciimg_stack.shape)

    # Determine the wavelength grid that we will use for the current slit/order
    wave_bins = get_wave_bins(thismask_stack, waveimg_stack, wave_grid)
    dspat_bins, dspat_stack = get_spat_bins(thismask_stack, ref_trace_stack)

    sci_list = [weights_stack, sciimg_stack, sciimg_stack - skymodel_stack, tilts_stack,
                waveimg_stack, dspat_stack]
    var_list = [utils.calc_ivar(sciivar_stack)]

    sci_list_rebin, var_list_rebin, norm_rebin_stack, nsmp_rebin_stack \
            = rebin2d(wave_bins, dspat_bins, waveimg_stack, dspat_stack, thismask_stack,
                      inmask_stack, sci_list, var_list)

    # Now compute the final stack with sigma clipping
    sigrej = 3.0
    maxiters = 10
    # sci_list_rebin[0] = rebinned weights image stack
    # sci_list_rebin[1:] = stacks of images that we want to weighted combine
    # sci_list_rebin[2] = rebinned sciimg-sky_model images that we used for the sigma clipping
    sci_list_out, var_list_out, outmask, nused \
            = weighted_combine(sci_list_rebin[0], sci_list_rebin[1:], var_list_rebin,
                               norm_rebin_stack != 0, sigma_clip=True,
                               sigma_clip_stack=sci_list_rebin[2], sigrej=sigrej,
                               maxiters=maxiters)
    sciimg, imgminsky, tilts, waveimg, dspat = sci_list_out
    sciivar = utils.calc_ivar(var_list_out[0])

    # Compute the midpoints vectors, and lower/upper bins of the rectified image
    wave_mid = ((wave_bins + np.roll(wave_bins,1))/2.0)[1:]
    wave_min = wave_bins[:-1]
    wave_max = wave_bins[1:]
    dspat_mid = ((dspat_bins + np.roll(dspat_bins,1))/2.0)[1:]

    # Interpolate the dspat images wherever the coadds are masked
    # because a given pixel was not sampled. This is done because the
    # dspat image is not allowed to have holes if it is going to work
    # with local_skysub_extract
    nspec_coadd, nspat_coadd = imgminsky.shape
    spat_img_coadd, spec_img_coadd = np.meshgrid(np.arange(nspat_coadd), np.arange(nspec_coadd))

    if np.any(np.invert(outmask)):
        points_good = np.stack((spec_img_coadd[outmask], spat_img_coadd[outmask]), axis=1)
        points_bad = np.stack((spec_img_coadd[np.invert(outmask)],
                                spat_img_coadd[np.invert(outmask)]), axis=1)
        values_dspat = dspat[outmask]
        dspat_bad = scipy.interpolate.griddata(points_good, values_dspat, points_bad,
                                               method='cubic')
        dspat[np.invert(outmask)] = dspat_bad
        # Points outside the convex hull of the data are set to nan. We
        # identify those and simply assume them values from the
        # dspat_img_fake, which is what dspat would be on a regular
        # perfectly rectified image grid.
        nanpix = np.isnan(dspat)
        if np.any(nanpix):
            dspat_img_fake = spat_img_coadd + dspat_mid[0]
            dspat[nanpix] = dspat_img_fake[nanpix]

    return dict(wave_bins=wave_bins, dspat_bins=dspat_bins, wave_mid=wave_mid, wave_min=wave_min,
                wave_max=wave_max, dspat_mid=dspat_mid, sciimg=sciimg, sciivar=sciivar,
                imgminsky=imgminsky, outmask=outmask, nused=nused, tilts=tilts, waveimg=waveimg,
                dspat=dspat, nspec=imgminsky.shape[0], nspat=imgminsky.shape[1])


def img_list_error_check(sci_list, var_list):
    """
    Utility routine for dealing dealing with lists of image stacks for rebin2d and weigthed_combine routines below. This
    routine checks that the images sizes are correct and routines the shape of the image stacks.
    Args:
        sci_list: list
            List of  float ndarray images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be combined with the  weights, inmask_stack, and possibly sigma clipping
        var_list: list
            List of  float ndarray variance images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be combined with proper erorr propagation, i.e.
            using the  weights**2, inmask_stack, and possibly sigma clipping

    Returns:
        shape: tuple
            The shapes of the image stacks, (nimgs, nspec, nspat)

    """
    shape_sci_list = []
    for img in sci_list:
        shape_sci_list.append(img.shape)
        if img.ndim < 2:
            msgs.error('Dimensionality of an image in sci_list is < 2')

    shape_var_list = []
    for img in var_list:
        shape_var_list.append(img.shape)
        if img.ndim < 2:
            msgs.error('Dimensionality of an image in var_list is < 2')

    for isci in shape_sci_list:
        if isci != shape_sci_list[0]:
            msgs.error('An image in sci_list have different dimensions')
        for ivar in shape_var_list:
            if ivar != shape_var_list[0]:
                msgs.error('An image in var_list have different dimensions')
            if isci != ivar:
                msgs.error('An image in sci_list had different dimensions than an image in var_list')

    shape = shape_sci_list[0]

    return shape

def rebin2d(spec_bins, spat_bins, waveimg_stack, spatimg_stack, thismask_stack, inmask_stack, sci_list, var_list):
    """
    Rebin a set of images and propagate variance onto a new spectral and spatial grid. This routine effectively
    "recitifies" images using np.histogram2d which is extremely fast and effectiveluy performs
    nearest grid point interpolation.

    Args:
        spec_bins: float ndarray, shape = (nspec_rebin)
           Spectral bins to rebin to.
        spat_bins: float ndarray, shape = (nspat_rebin)
           Spatial bins to rebin to.
        waveimg_stack: float ndarray, shape = (nimgs, nspec, nspat)
            Stack of nimgs wavelength images with shape = (nspec, nspat) each
        spatimg_stack: float ndarray, shape = (nimgs, nspec, nspat)
            Stack of nimgs spatial position images with shape = (nspec, nspat) each
        thismask_stack: bool ndarray, shape = (nimgs, nspec, nspat)
            Stack of nimgs images with shape = (nspec, nspat) indicating the locatons on the pixels on an image that
            are on the slit in question.
        inmask_stack: bool ndarray, shape = (nimgs, nspec, nspat)
            Stack of nimgs images with shape = (nspec, nspat) indicating which pixels on an image are masked.
            True = Good, False = Bad
        sci_list: list
            List of  float ndarray images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be rebinned onto the new spec_bins, spat_bins
        var_list: list
            List of  float ndarray variance images (each being an image stack with shape (nimgs, nspec, nspat))
            which are to be rebbinned with proper erorr propagation

    Returns:
        sci_list_out: list
           The list of ndarray rebinned images with new shape (nimgs, nspec_rebin, nspat_rebin)
        var_list_out: list
           The list of ndarray rebinned variance images with correct error propagation with shape
           (nimgs, nspec_rebin, nspat_rebin)
        norm_rebin_stack: int ndarray, shape (nimgs, nspec_rebin, nspat_rebin)
           An image stack indicating the integer occupation number of a given pixel. In other words, this number would be zero
           for empty bins, one for bins that were populated by a single pixel, etc. This image takes the input
           inmask_stack into account. The output mask for each image can be formed via
           outmask_rebin_satck = (norm_rebin_stack > 0)
        nsmp_rebin_stack: int ndarray, shape (nimgs, nspec_rebin, nspat_rebin)
           An image stack indicating the integer occupation number of a given pixel taking only the thismask_stack into
           account, but taking the inmask_stack into account. This image is mainly constructed for bookeeping purposes,
           as it represents the number of times each pixel in the rebin image was populated taking only the "geometry"
           of the rebinning into account (i.e. the thismask_stack), but not the masking (inmask_stack).

    """

    shape = img_list_error_check(sci_list, var_list)
    nimgs = shape[0]
    # allocate the output mages
    nspec_rebin = spec_bins.size - 1
    nspat_rebin = spat_bins.size - 1
    shape_out = (nimgs, nspec_rebin, nspat_rebin)
    nsmp_rebin_stack = np.zeros(shape_out)
    norm_rebin_stack = np.zeros(shape_out)
    sci_list_out = []
    for ii in range(len(sci_list)):
        sci_list_out.append(np.zeros(shape_out))
    var_list_out = []
    for jj in range(len(var_list)):
        var_list_out.append(np.zeros(shape_out))

    for img in range(nimgs):
        # This fist image is purely for bookeeping purposes to determine the number of times each pixel
        # could have been sampled
        thismask = thismask_stack[img, :, :]
        spec_rebin_this = waveimg_stack[img, :, :][thismask]
        spat_rebin_this = spatimg_stack[img, :, :][thismask]

        nsmp_rebin_stack[img, :, :], spec_edges, spat_edges = np.histogram2d(spec_rebin_this, spat_rebin_this,
                                                               bins=[spec_bins, spat_bins], density=False)

        finmask = thismask & inmask_stack[img,:,:]
        spec_rebin = waveimg_stack[img, :, :][finmask]
        spat_rebin = spatimg_stack[img, :, :][finmask]
        norm_img, spec_edges, spat_edges = np.histogram2d(spec_rebin, spat_rebin,
                                                          bins=[spec_bins, spat_bins], density=False)
        norm_rebin_stack[img, :, :] = norm_img

        # Rebin the science images
        for indx, sci in enumerate(sci_list):
            weigh_sci, spec_edges, spat_edges = np.histogram2d(spec_rebin, spat_rebin,
                                                               bins=[spec_bins, spat_bins], density=False,
                                                               weights=sci[img,:,:][finmask])
            sci_list_out[indx][img, :, :] = (norm_img > 0.0) * weigh_sci/(norm_img + (norm_img == 0.0))

        # Rebin the variance images, note the norm_img**2 factor for correct error propagation
        for indx, var in enumerate(var_list):
            weigh_var, spec_edges, spat_edges = np.histogram2d(spec_rebin, spat_rebin,
                                                               bins=[spec_bins, spat_bins], density=False,
                                                               weights=var[img, :, :][finmask])
            var_list_out[indx][img, :, :] = (norm_img > 0.0)*weigh_var/(norm_img + (norm_img == 0.0))**2


    return sci_list_out, var_list_out, norm_rebin_stack.astype(int), nsmp_rebin_stack.astype(int)

# TODO Break up into separate methods?

class Coadd2d(object):

    """
    Main routine to run the extraction for 2d coadds.

    Algorithm steps are as follows:
        - Fill this in.

    This performs 2d coadd specific tasks, and then also performs some
    of the tasks analogous to the pypeit.extract_one method. Docs coming
    soon....

    Args:
        stack_dict:
        master_dir:
        det (int):
        samp_fact: float
           sampling factor to make the wavelength grid finer or coarser.  samp_fact > 1.0 oversamples (finer),
           samp_fact < 1.0 undersamples (coarser)
        ir_redux:
        par:
        show:
        show_peaks:

    Returns:

    """

    def __init__(self, spec2d_files, spectrograph, det=1, offsets=None, weights='auto', sn_smooth_npix=None, par=None,
                 ir_redux=False, show=False, show_peaks=False, debug_offsets=False, debug=False, **kwargs_wave):
        """

        Args:
            spec2d_files:
            det:
            offsets (ndarray): default=None
                Spatial offsets to be applied to each image before coadding. For the default mode of None, images
                are registered automatically using the trace of the brightest object.
            weights (str, list or ndarray):
                Mode for the weights used to coadd images. Options are 'auto' (default), 'uniform', or list/array of
                weights with shape = (nexp,) can be input and will be applied to the image. Note 'auto' is not allowed
                if offsets are input, and if set this will cause an exception.
            sn_smooth_npix:
            ir_redux:
            par:
            std:
            show:
            show_peaks:
            debug:
            **kwargs_wave:
        """

        ## Use Cases:
        #  1) offsets is None -- auto compute offsets from brightest object, so then default to auto_weights=True
        #  2) offsets not None, weights = None (uniform weighting) or weights is not None (input weights)
        #  3) offsets not None, auto_weights=True (Do not support)
        if offsets is not None and 'auto' in weights:
            msgs.error("Automatic weights cannot be computed for input offsets. "
                       "Set weights='uniform' or input an array of weights with shape (nexp,)")
        self.spec2d_files = spec2d_files
        self.spectrograph = spectrograph
        self.det = det
        self.offsets = offsets
        self.weights = weights
        self.ir_redux = ir_redux
        self.show = show
        self.show_peaks = show_peaks
        self.debug_offsets = debug_offsets
        self.debug = debug
        self.stack_dict = None
        self.psuedo_dict = None

        self.objid_bri = None
        self.slitid_bri  = None
        self.snr_bar_bri = None


        # Load the stack_dict
        self.stack_dict = self.load_coadd2d_stacks(self.spec2d_files)
        self.pypeline = self.spectrograph.pypeline
        self.par = self.spectrograph.default_pypeit_par() if par is None else par


        # Check that there are the same number of slits on every exposure
        nslits_list = []
        for tslits_dict in self.stack_dict['tslits_dict_list']:
            nspec, nslits_now = tslits_dict['slit_left'].shape
            nslits_list.append(nslits_now)
        if not len(set(nslits_list))==1:
            msgs.error('Not all of your exposures have the same number of slits. Check your inputs')
        self.nslits = nslits_list[0]
        self.nexp = len(self.stack_dict['specobjs_list'])
        self.nspec = nspec
        self.binning = np.array([self.stack_dict['tslits_dict_list'][0]['binspectral'],
                                 self.stack_dict['tslits_dict_list'][0]['binspatial']])

        # If smoothing is not input, smooth by 10% of the spectral dimension
        self.sn_smooth_npix = sn_smooth_npix if sn_smooth_npix is not None else 0.1*self.nspec



    def create_psuedo_image(self, coadd_list):


        nspec_vec = np.zeros(self.nslits,dtype=int)
        nspat_vec = np.zeros(self.nslits,dtype=int)
        for islit, cdict in enumerate(coadd_list):
            nspec_vec[islit]=cdict['nspec']
            nspat_vec[islit]=cdict['nspat']

        # Determine the size of the psuedo image
        nspat_pad = 10
        nspec_psuedo = nspec_vec.max()
        nspat_psuedo = np.sum(nspat_vec) + (self.nslits + 1)*nspat_pad
        spec_vec_psuedo = np.arange(nspec_psuedo)
        shape_psuedo = (nspec_psuedo, nspat_psuedo)
        imgminsky_psuedo = np.zeros(shape_psuedo)
        sciivar_psuedo = np.zeros(shape_psuedo)
        waveimg_psuedo = np.zeros(shape_psuedo)
        tilts_psuedo = np.zeros(shape_psuedo)
        spat_img_psuedo = np.zeros(shape_psuedo)
        nused_psuedo = np.zeros(shape_psuedo, dtype=int)
        inmask_psuedo = np.zeros(shape_psuedo, dtype=bool)
        wave_mid = np.zeros((nspec_psuedo, self.nslits))
        wave_mask = np.zeros((nspec_psuedo, self.nslits),dtype=bool)
        wave_min = np.zeros((nspec_psuedo, self.nslits))
        wave_max = np.zeros((nspec_psuedo, self.nslits))
        dspat_mid = np.zeros((nspat_psuedo, self.nslits))

        spat_left = nspat_pad
        slit_left = np.zeros((nspec_psuedo, self.nslits))
        slit_righ = np.zeros((nspec_psuedo, self.nslits))
        spec_min1 = np.zeros(self.nslits)
        spec_max1 = np.zeros(self.nslits)

        nspec_grid = self.wave_grid_mid.size
        for islit, coadd_dict in enumerate(coadd_list):
            spat_righ = spat_left + nspat_vec[islit]
            ispec = slice(0,nspec_vec[islit])
            ispat = slice(spat_left,spat_righ)
            imgminsky_psuedo[ispec, ispat] = coadd_dict['imgminsky']
            sciivar_psuedo[ispec, ispat] = coadd_dict['sciivar']
            waveimg_psuedo[ispec, ispat] = coadd_dict['waveimg']
            tilts_psuedo[ispec, ispat] = coadd_dict['tilts']
            # spat_img_psuedo is the sub-pixel image position on the rebinned psuedo image
            inmask_psuedo[ispec, ispat] = coadd_dict['outmask']
            image_temp = (coadd_dict['dspat'] -  coadd_dict['dspat_mid'][0] + spat_left)*coadd_dict['outmask']
            spat_img_psuedo[ispec, ispat] = image_temp
            nused_psuedo[ispec, ispat] = coadd_dict['nused']
            wave_min[ispec, islit] = coadd_dict['wave_min']
            wave_max[ispec, islit] = coadd_dict['wave_max']
            wave_mid[ispec, islit] = coadd_dict['wave_mid']
            wave_mask[ispec, islit] = True
            # Fill in the rest of the wave_mid with the corresponding points in the wave_grid
            #wave_this = wave_mid[wave_mask[:,islit], islit]
            #ind_upper = np.argmin(np.abs(self.wave_grid_mid - wave_this.max())) + 1
            #if nspec_vec[islit] != nspec_psuedo:
            #    wave_mid[nspec_vec[islit]:, islit] = self.wave_grid_mid[ind_upper:ind_upper + (nspec_psuedo-nspec_vec[islit])]


            dspat_mid[ispat, islit] = coadd_dict['dspat_mid']
            slit_left[:,islit] = np.full(nspec_psuedo, spat_left)
            slit_righ[:,islit] = np.full(nspec_psuedo, spat_righ)
            spec_max1[islit] = nspec_vec[islit]-1
            spat_left = spat_righ + nspat_pad

        slitcen = (slit_left + slit_righ)/2.0
        tslits_dict_psuedo = dict(slit_left=slit_left, slit_righ=slit_righ, slitcen=slitcen,
                                  nspec=nspec_psuedo, nspat=nspat_psuedo, pad=0,
                                  nslits = self.nslits, binspectral=1, binspatial=1, spectrograph=self.spectrograph.spectrograph,
                                  spec_min=spec_min1, spec_max=spec_max1,
                                  maskslits=np.zeros(slit_left.shape[1], dtype=np.bool))

        slitmask_psuedo = pixels.tslits2mask(tslits_dict_psuedo)
        # This is a kludge to deal with cases where bad wavelengths result in large regions where the slit is poorly sampled,
        # which wreaks havoc on the local sky-subtraction
        min_slit_frac = 0.70
        spec_min = np.zeros(self.nslits)
        spec_max = np.zeros(self.nslits)
        for islit in range(self.nslits):
            slit_width = np.sum(inmask_psuedo*(slitmask_psuedo == islit),axis=1)
            slit_width_img = np.outer(slit_width, np.ones(nspat_psuedo))
            med_slit_width = np.median(slit_width_img[slitmask_psuedo == islit])
            nspec_eff = np.sum(slit_width > min_slit_frac*med_slit_width)
            nsmooth = int(np.fmax(np.ceil(nspec_eff*0.02),10))
            slit_width_sm = scipy.ndimage.filters.median_filter(slit_width, size=nsmooth, mode='reflect')
            igood = (slit_width_sm > min_slit_frac*med_slit_width)
            spec_min[islit] = spec_vec_psuedo[igood].min()
            spec_max[islit] = spec_vec_psuedo[igood].max()
            bad_pix = (slit_width_img < min_slit_frac*med_slit_width) & (slitmask_psuedo == islit)
            inmask_psuedo[bad_pix] = False

        # Update with tslits_dict_psuedo
        tslits_dict_psuedo['spec_min'] = spec_min
        tslits_dict_psuedo['spec_max'] = spec_max

        psuedo_dict = dict(nspec=nspec_psuedo, nspat=nspat_psuedo, imgminsky=imgminsky_psuedo, sciivar=sciivar_psuedo,
                           inmask=inmask_psuedo, tilts=tilts_psuedo,
                           waveimg=waveimg_psuedo, spat_img = spat_img_psuedo,
                           tslits_dict=tslits_dict_psuedo,
                           wave_mask=wave_mask, wave_mid=wave_mid, wave_min=wave_min, wave_max=wave_max)

        return psuedo_dict

    def reduce(self, psuedo_dict, show=None, show_peaks=None):

        show = self.show if show is None else show
        show_peaks = self.show_peaks if show_peaks is None else show_peaks

        # Generate a ScienceImage
        sciImage = scienceimage.ScienceImage(self.spectrograph, self.det,
                                                      self.par['scienceframe']['process'],
                                                      psuedo_dict['imgminsky'],
                                                      psuedo_dict['sciivar'],
                                                      np.zeros_like(psuedo_dict['inmask']),  # Dummy bpm
                                                      rn2img=np.zeros_like(psuedo_dict['inmask']),  # Dummy rn2img
                                                      crmask=np.invert(psuedo_dict['inmask']))
        slitmask_psuedo = pixels.tslits2mask(psuedo_dict['tslits_dict'])
        sciImage.build_mask(slitmask=slitmask_psuedo)

        # Make changes to parset specific to 2d coadds
        parcopy = copy.deepcopy(self.par)
        parcopy['scienceimage']['trace_npoly'] = 3        # Low order traces since we are rectified
        #parcopy['scienceimage']['find_extrap_npoly'] = 1  # Use low order for trace extrapolation
        redux = reduce.instantiate_me(sciImage, self.spectrograph, psuedo_dict['tslits_dict'], parcopy, psuedo_dict['tilts'],
                                      ir_redux=self.ir_redux, objtype = 'science', det=self.det, binning=self.binning)

        if show:
            redux.show('image', image=psuedo_dict['imgminsky']*(sciImage.mask == 0), chname = 'imgminsky', slits=True, clear=True)
        # Object finding
        sobjs_obj, nobj, skymask_init = redux.find_objects(sciImage.image, ir_redux=self.ir_redux, show_peaks=show_peaks, show=show)
        # Local sky-subtraction
        global_sky_psuedo = np.zeros_like(psuedo_dict['imgminsky']) # No global sky for co-adds since we go straight to local
        skymodel_psuedo, objmodel_psuedo, ivarmodel_psuedo, outmask_psuedo, sobjs = redux.local_skysub_extract(
            psuedo_dict['waveimg'], global_sky_psuedo, sobjs_obj, spat_pix=psuedo_dict['spat_img'], model_noise=False,
            show_profile=show, show=show)

        if self.ir_redux:
            sobjs.purge_neg()

        # Add the information about the fixed wavelength grid to the sobjs
        for spec in sobjs:
            spec.boxcar['WAVE_GRID_MASK'], spec.optimal['WAVE_GRID_MASK'] =  [psuedo_dict['wave_mask'][:,spec.slitid]]*2
            spec.boxcar['WAVE_GRID'], spec.optimal['WAVE_GRID'] =  [psuedo_dict['wave_mid'][:,spec.slitid]]*2
            spec.boxcar['WAVE_GRID_MIN'], spec.optimal['WAVE_GRID_MIN'] = [psuedo_dict['wave_min'][:,spec.slitid]]*2
            spec.boxcar['WAVE_GRID_MAX'], spec.optimal['WAVE_GRID_MAX']= [psuedo_dict['wave_max'][:,spec.slitid]]*2

        # Add the rest to the psuedo_dict
        psuedo_dict['skymodel'] = skymodel_psuedo
        psuedo_dict['objmodel'] = objmodel_psuedo
        psuedo_dict['ivarmodel'] = ivarmodel_psuedo
        psuedo_dict['outmask'] = outmask_psuedo
        psuedo_dict['sobjs'] = sobjs
        self.psuedo_dict=psuedo_dict

        return psuedo_dict['imgminsky'], psuedo_dict['sciivar'], skymodel_psuedo, objmodel_psuedo, ivarmodel_psuedo, outmask_psuedo, sobjs


    def save_masters(self, master_dir):

        # Write out the psuedo master files to disk
        master_key_dict = self.stack_dict['master_key_dict']

        # TODO: These saving operations are a temporary kludge
        waveImage = WaveImage(None, None, None, self.spectrograph,  # spectrograph is needed for header
                              None, None, master_key=master_key_dict['arc'],
                              master_dir=master_dir)
        waveImage.save(image=self.psuedo_dict['waveimg'])

        traceSlits = TraceSlits(self.spectrograph, None,   # Spectrograph is needed for header
                                master_key=master_key_dict['trace'], master_dir=master_dir)
        traceSlits.save(tslits_dict=self.psuedo_dict['tslits_dict'])


    def snr_report(self, snr_bar, slitid=None):

        # Print out a report on the SNR
        msg_string = msgs.newline() + '-------------------------------------'
        msg_string += msgs.newline() + '  Summary for highest S/N object'
        if slitid is not None:
            msg_string += msgs.newline() + '      found on slitid = {:d}            '.format(slitid)
        msg_string += msgs.newline() + '-------------------------------------'
        msg_string += msgs.newline() + '           exp#        S/N'
        for iexp, snr in enumerate(snr_bar):
            msg_string += msgs.newline() + '            {:d}         {:5.2f}'.format(iexp, snr)

        msg_string += msgs.newline() + '-------------------------------------'
        msgs.info(msg_string)

    def get_good_slits(self, only_slits):

        only_slits = [only_slits] if (only_slits is not None and
                                        isinstance(only_slits, (int, np.int, np.int64, np.int32))) else only_slits
        good_slits = np.arange(self.nslits) if only_slits is None else only_slits
        return good_slits

    def offset_slit_cen(self, slitid, offsets):

        nexp = len(offsets)
        tslits_dict_list = self.stack_dict['tslits_dict_list']
        nspec, nslits = tslits_dict_list[0]['slit_left'].shape
        ref_trace_stack = np.zeros((nspec, nexp))
        for iexp, tslits_dict in enumerate(tslits_dict_list):
            ref_trace_stack[:, iexp] = (tslits_dict['slit_left'][:, slitid] +
                                        tslits_dict['slit_righ'][:, slitid])/2.0 + offsets[iexp]
        return ref_trace_stack

    def coadd(self, only_slits=None):

        only_slits = [only_slits] if (only_slits is not None and
                                      isinstance(only_slits, (int, np.int, np.int64, np.int32))) else only_slits
        good_slits = np.arange(self.nslits) if only_slits is None else only_slits

        coadd_list = []
        for islit in good_slits:
            msgs.info('Performing 2d coadd for slit: {:d}/{:d}'.format(islit, self.nslits - 1))
            ref_trace_stack = self.reference_trace_stack(islit, offsets=self.offsets, objid=self.objid_bri)
            thismask_stack = self.stack_dict['slitmask_stack'] == islit
            # This one line deals with the different weighting strategies between MultiSlit echelle. Otherwise, we
            # would need to copy this method twice in the subclasses
            if 'auto_echelle' in self.use_weights:
                rms_sn, weights = optimal_weights(self.stack_dict['specobjs_list'], islit, self.objid_bri,
                                                  self.sn_smooth_npix)
            else:
                weights = self.use_weights
            # Perform the 2d coadd
            coadd_dict = compute_coadd2d(ref_trace_stack, self.stack_dict['sciimg_stack'],
                                           self.stack_dict['sciivar_stack'],
                                           self.stack_dict['skymodel_stack'], self.stack_dict['mask_stack'] == 0,
                                           self.stack_dict['tilts_stack'], thismask_stack,
                                           self.stack_dict['waveimg_stack'],
                                           self.wave_grid, weights=weights)
            coadd_list.append(coadd_dict)

        return coadd_list

    def get_wave_grid(self, **kwargs_wave):

        nobjs_tot = np.array([len(spec) for spec in self.stack_dict['specobjs_list']]).sum()
        waves = np.zeros((self.nspec, nobjs_tot))
        masks = np.zeros_like(waves, dtype=bool)
        indx = 0
        for spec_this in self.stack_dict['specobjs_list']:
            for spec in spec_this:
                waves[:, indx] = spec.optimal['WAVE']
                masks[:, indx] = spec.optimal['MASK']
                indx += 1

        wave_grid, wave_grid_mid, dsamp = coadd1d.get_wave_grid(waves, masks=masks, **kwargs_wave)

        return wave_grid, wave_grid_mid, dsamp

    def load_coadd2d_stacks(self, spec2d_files):
        """

        Args:
            spec2d_files: list
               List of spec2d filenames
            det: int
               detector in question

        Returns:
            stack_dict: dict
               Dictionary containing all the images and keys required for perfomring 2d coadds.

        """

        # Get the detector string
        sdet = parse.get_dnum(self.det, prefix=False)

        # Get the master dir

        redux_path = os.getcwd()

        # Grab the files
        head2d_list = []
        tracefiles = []
        waveimgfiles = []
        tiltfiles = []
        spec1d_files = []
        for f in spec2d_files:
            head = fits.getheader(f)
            if os.path.exists(head['PYPMFDIR']):
                master_path = head['PYPMFDIR']
            else:
                master_dir = os.path.basename(head['PYPMFDIR'])
                master_path = os.path.join(os.path.split(os.path.split(f)[0])[0], master_dir)

            trace_key = '{0}_{1:02d}'.format(head['TRACMKEY'], self.det)
            wave_key = '{0}_{1:02d}'.format(head['ARCMKEY'], self.det)

            head2d_list.append(head)
            spec1d_files.append(f.replace('spec2d', 'spec1d'))
            tracefiles.append(os.path.join(master_path,
                                           MasterFrame.construct_file_name('Trace', trace_key)))
            waveimgfiles.append(os.path.join(master_path,
                                             MasterFrame.construct_file_name('Wave', wave_key)))
            tiltfiles.append(os.path.join(master_path,
                                          MasterFrame.construct_file_name('Tilts', wave_key)))

        nfiles = len(spec2d_files)

        specobjs_list = []
        head1d_list = []
        tslits_dict_list = []
        # TODO Sort this out with the correct detector extensions etc.
        # Read in the image stacks
        for ifile in range(nfiles):
            #waveimg = WaveImage.load_from_file(waveimgfiles[ifile])  # JXP
            waveimg = WaveImage.from_master_file(waveimgfiles[ifile]).image
            #tilts = WaveTilts.load_from_file(tiltfiles[ifile])
            tilts = WaveTilts.from_master_file(tiltfiles[ifile]).tilts_dict
            hdu = fits.open(spec2d_files[ifile])
            # One detector, sky sub for now
            names = [hdu[i].name for i in range(len(hdu))]
            # science image
            try:
                exten = names.index('DET{:s}-PROCESSED'.format(sdet))
            except:  # Backwards compatability
                det_error_msg(exten, sdet)
            sciimg = hdu[exten].data
            # skymodel
            try:
                exten = names.index('DET{:s}-SKY'.format(sdet))
            except:  # Backwards compatability
                det_error_msg(exten, sdet)
            skymodel = hdu[exten].data
            # Inverse variance model
            try:
                exten = names.index('DET{:s}-IVARMODEL'.format(sdet))
            except ValueError:  # Backwards compatability
                det_error_msg(exten, sdet)
            sciivar = hdu[exten].data
            # Mask
            try:
                exten = names.index('DET{:s}-MASK'.format(sdet))
            except ValueError:  # Backwards compatability
                det_error_msg(exten, sdet)
            mask = hdu[exten].data
            if ifile == 0:
                # the two shapes accomodate the possibility that waveimg and tilts are binned differently
                shape_wave = (nfiles, waveimg.shape[0], waveimg.shape[1])
                shape_sci = (nfiles, sciimg.shape[0], sciimg.shape[1])
                waveimg_stack = np.zeros(shape_wave, dtype=float)
                tilts_stack = np.zeros(shape_wave, dtype=float)
                sciimg_stack = np.zeros(shape_sci, dtype=float)
                skymodel_stack = np.zeros(shape_sci, dtype=float)
                sciivar_stack = np.zeros(shape_sci, dtype=float)
                mask_stack = np.zeros(shape_sci, dtype=float)
                slitmask_stack = np.zeros(shape_sci, dtype=float)

            # Slit Traces and slitmask
            tslits_dict, _ = TraceSlits.load_from_file(tracefiles[ifile])
            tslits_dict_list.append(tslits_dict)
            slitmask = pixels.tslits2mask(tslits_dict)
            slitmask_stack[ifile, :, :] = slitmask
            waveimg_stack[ifile, :, :] = waveimg
            tilts_stack[ifile, :, :] = tilts['tilts']
            sciimg_stack[ifile, :, :] = sciimg
            sciivar_stack[ifile, :, :] = sciivar
            mask_stack[ifile, :, :] = mask
            skymodel_stack[ifile, :, :] = skymodel

            # Specobjs
            head1d_list.append(head)
            sobjs, head = load.load_specobjs(spec1d_files[ifile])
            this_det = sobjs.det == self.det
            specobjs_list.append(sobjs[this_det])

        # slitmask_stack = np.einsum('i,jk->ijk', np.ones(nfiles), slitmask)

        # Fill the master key dict
        head2d = head2d_list[0]
        master_key_dict = {}
        master_key_dict['frame'] = head2d['FRAMMKEY'] + '_{:02d}'.format(self.det)
        master_key_dict['bpm'] = head2d['BPMMKEY'] + '_{:02d}'.format(self.det)
        master_key_dict['bias'] = head2d['BIASMKEY'] + '_{:02d}'.format(self.det)
        master_key_dict['arc'] = head2d['ARCMKEY'] + '_{:02d}'.format(self.det)
        master_key_dict['trace'] = head2d['TRACMKEY'] + '_{:02d}'.format(self.det)
        master_key_dict['flat'] = head2d['FLATMKEY'] + '_{:02d}'.format(self.det)

        # TODO In the future get this stuff from the headers once data model finalized
        spectrograph = util.load_spectrograph(tslits_dict['spectrograph'])

        stack_dict = dict(specobjs_list=specobjs_list, tslits_dict_list=tslits_dict_list,
                          slitmask_stack=slitmask_stack,
                          sciimg_stack=sciimg_stack, sciivar_stack=sciivar_stack,
                          skymodel_stack=skymodel_stack, mask_stack=mask_stack,
                          tilts_stack=tilts_stack, waveimg_stack=waveimg_stack,
                          head1d_list=head1d_list, head2d_list=head2d_list,
                          redux_path=redux_path,
                          master_key_dict=master_key_dict,
                          spectrograph=spectrograph.spectrograph,
                          pypeline=spectrograph.pypeline)

        return stack_dict

# Multislit can coadd with:
# 1) input offsets or if offsets is None, it will find the brightest trace and compute them
# 2) specified weights, or if weights is None and auto_weights=True, it will compute weights using the brightest object

# Echelle can either stack with:
# 1) input offsets or if offsets is None, it will find the objid of brightest trace and stack all orders relative to the trace of this object.
# 2) specified weights, or if weights is None and auto_weights=True,
#    it will use wavelength dependent weights determined from the spectrum of the brightest objects objid on each order

class MultiSlitCoadd2d(Coadd2d):
    """
    Child of Coadd2d for Multislit and Longslit reductions

        # Multislit can coadd with:
        # 1) input offsets or if offsets is None, it will find the brightest trace and compute them
        # 2) specified weights, or if weights is None and auto_weights=True, it will compute weights using the brightest object


    """
    def __init__(self, spec2d_files, spectrograph, det=1, offsets=None, weights='auto', sn_smooth_npix=None,
                 ir_redux=False, par=None, show=False, show_peaks=False, debug_offsets=False, debug=False, **kwargs_wave):
        super(MultiSlitCoadd2d, self).__init__(spec2d_files, spectrograph, det=det, offsets=offsets, weights=weights,
                                        sn_smooth_npix=sn_smooth_npix, ir_redux=ir_redux, par=par,
                                        show=show, show_peaks=show_peaks, debug_offsets=debug_offsets,
                                        debug=debug, **kwargs_wave)


        ## Use Cases:
        #  1) offsets is None -- auto compute offsets from brightest object, so then default to auto_weights=True
        #  2) offsets not None, weights = None (uniform weighting) or weights is not None (input weights)
        #  3) offsets not None, auto_weights=True (Do not support)

        # Default wave_method for Multislit is linear
        kwargs_wave['wave_method'] = 'linear' if 'wave_method' not in kwargs_wave else kwargs_wave['wave_method']
        self.wave_grid, self.wave_grid_mid, self.dsamp = self.get_wave_grid(**kwargs_wave)

        if offsets is None:
            self.objid_bri, self.slitid_bri, self.snr_bar_bri, self.offsets = self.compute_offsets()

        self.use_weights = self.parse_weights(weights)

    def parse_weights(self, weights):

        if 'auto' in weights:
            rms_sn, use_weights = optimal_weights(self.stack_dict['specobjs_list'], self.slitid_bri, self.objid_bri,
                                                  self.sn_smooth_npix, const_weights=True)
            return use_weights
        elif 'uniform' in weights:
            return 'uniform'
        elif isinstance(weights, (list, np.ndarray)):
            if len(weights) != self.nexp:
                msgs.error('If weights are input it must be a list/array with same number of elements as exposures')
            return weights
        else:
            msgs.error('Unrecognized format for weights')

    def compute_offsets(self):

        objid_bri, slitid_bri, snr_bar_bri = self.get_brightest_obj(self.stack_dict['specobjs_list'], self.nslits)
        msgs.info('Determining offsets using brightest object on slit: {:d} with avg SNR={:5.2f}'.format(slitid_bri,np.mean(snr_bar_bri)))
        thismask_stack = self.stack_dict['slitmask_stack'] == slitid_bri
        trace_stack_bri = np.zeros((self.nspec, self.nexp))
        # TODO Need to think abbout whether we have multiple tslits_dict for each exposure or a single one
        for iexp in range(self.nexp):
            trace_stack_bri[:,iexp] = (self.stack_dict['tslits_dict_list'][iexp]['slit_left'][:,slitid_bri] +
                                       self.stack_dict['tslits_dict_list'][iexp]['slit_righ'][:,slitid_bri])/2.0
        # Determine the wavelength grid that we will use for the current slit/order
        wave_bins = get_wave_bins(thismask_stack, self.stack_dict['waveimg_stack'], self.wave_grid)
        dspat_bins, dspat_stack = get_spat_bins(thismask_stack, trace_stack_bri)

        sci_list = [self.stack_dict['sciimg_stack'] - self.stack_dict['skymodel_stack']]
        var_list = []

        sci_list_rebin, var_list_rebin, norm_rebin_stack, nsmp_rebin_stack = rebin2d(
            wave_bins, dspat_bins, self.stack_dict['waveimg_stack'], dspat_stack, thismask_stack,
            (self.stack_dict['mask_stack'] == 0), sci_list, var_list)
        thismask = np.ones_like(sci_list_rebin[0][0,:,:],dtype=bool)
        nspec_psuedo, nspat_psuedo = thismask.shape
        slit_left = np.full(nspec_psuedo, 0.0)
        slit_righ = np.full(nspec_psuedo, nspat_psuedo)
        inmask = norm_rebin_stack > 0
        traces_rect = np.zeros((nspec_psuedo, self.nexp))
        sobjs = specobjs.SpecObjs()
        #specobj_dict = {'setup': 'unknown', 'slitid': 999, 'orderindx': 999, 'det': self.det, 'objtype': 'unknown',
        #                'pypeline': 'MultiSLit' + '_coadd_2d'}
        for iexp in range(self.nexp):
            sobjs_exp, _ = extract.objfind(sci_list_rebin[0][iexp,:,:], thismask, slit_left, slit_righ,
                                           inmask=inmask[iexp,:,:], ir_redux=self.ir_redux,
                                           fwhm=self.par['scienceimage']['find_fwhm'],
                                           trim_edg=self.par['scienceimage']['find_trim_edge'],
                                           npoly_cont=self.par['scienceimage']['find_npoly_cont'],
                                           maxdev=self.par['scienceimage']['find_maxdev'],
                                           ncoeff=3, sig_thresh=10.0, nperslit=1,
                                           show_trace=self.debug_offsets, show_peaks=self.debug_offsets)
            sobjs.add_sobj(sobjs_exp)
            traces_rect[:, iexp] = sobjs_exp.trace_spat
        # Now deterimine the offsets. Arbitrarily set the zeroth trace to the reference
        med_traces_rect = np.median(traces_rect,axis=0)
        offsets = med_traces_rect[0] - med_traces_rect
        # Print out a report on the offsets
        msg_string = msgs.newline()  + '---------------------------------------------'
        msg_string += msgs.newline() + ' Summary of offsets for highest S/N object   '
        msg_string += msgs.newline() + '         found on slitid = {:d}              '.format(slitid_bri)
        msg_string += msgs.newline() + '---------------------------------------------'
        msg_string += msgs.newline() + '           exp#      offset                  '
        for iexp, off in enumerate(offsets):
            msg_string += msgs.newline() + '            {:d}        {:5.2f}'.format(iexp, off)

        msg_string += msgs.newline() + '-----------------------------------------------'
        msgs.info(msg_string)
        if self.debug_offsets:
            for iexp in range(self.nexp):
                plt.plot(traces_rect[:, iexp], linestyle='--', label='original trace')
                plt.plot(traces_rect[:, iexp] + offsets[iexp], label='shifted traces')
                plt.legend()
            plt.show()

        return objid_bri, slitid_bri, snr_bar_bri, offsets

    def get_brightest_obj(self, specobjs_list, nslits):

        """
        Utility routine to find the brightest object in each exposure given a specobjs_list for MultiSlit reductions.

        Parameters:
            specobjs_list: list
               List of SpecObjs objects.
        Optional Parameters:
            echelle: bool, default=True

        Returns:
            (objid, slitid, snr_bar)

            objid: ndarray, int, shape (len(specobjs_list),)
                Array of object ids representing the brightest object in each exposure
            slitid (int):
                Slit that highest S/N ratio object is on (only for pypeline=MultiSlit)
            snr_bar: ndarray, float, shape (len(list),)
                Average S/N over all the orders for this object

        """
        nexp = len(specobjs_list)
        nspec = specobjs_list[0][0].shape[0]

        slit_snr_max = np.full((nslits, nexp), -np.inf)
        objid_max = np.zeros((nslits, nexp), dtype=int)
        # Loop over each exposure, slit, find the brighest object on that slit for every exposure
        for iexp, sobjs in enumerate(specobjs_list):
            for islit in range(nslits):
                ithis = sobjs.slitid == islit
                nobj_slit = np.sum(ithis)
                if np.any(ithis):
                    objid_this = sobjs[ithis].objid
                    flux = np.zeros((nspec, nobj_slit))
                    ivar = np.zeros((nspec, nobj_slit))
                    wave = np.zeros((nspec, nobj_slit))
                    mask = np.zeros((nspec, nobj_slit), dtype=bool)
                    for iobj, spec in enumerate(sobjs[ithis]):
                        flux[:, iobj] = spec.optimal['COUNTS']
                        ivar[:, iobj] = spec.optimal['COUNTS_IVAR']
                        wave[:, iobj] = spec.optimal['WAVE']
                        mask[:, iobj] = spec.optimal['MASK']
                    rms_sn, weights = coadd1d.sn_weights(wave, flux, ivar, mask, None, const_weights=True)
                    imax = np.argmax(rms_sn)
                    slit_snr_max[islit, iexp] = rms_sn[imax]
                    objid_max[islit, iexp] = objid_this[imax]
        # Find the highest snr object among all the slits
        slit_snr = np.mean(slit_snr_max, axis=1)
        slitid = slit_snr.argmax()
        snr_bar_mean = slit_snr[slitid]
        snr_bar = slit_snr_max[slitid, :]
        objid = objid_max[slitid, :]
        if (snr_bar_mean == -np.inf):
            msgs.error('You do not appear to have a unique reference object that was traced as the highest S/N '
                       'ratio on the same slit of every exposure')

        self.snr_report(snr_bar, slitid=slitid)

        return objid, slitid, snr_bar

    def reference_trace_stack(self, slitid, offsets=None, objid=None):

        return self.offset_slit_cen(slitid, offsets)


class EchelleCoadd2d(Coadd2d):
    """
    Child of Coadd2d for Multislit and Longslit reductions

        # Echelle can either stack with:
        # 1) input offsets or if offsets is None, it will find the objid of brightest trace and stack all orders relative to the trace of this object.
        # 2) specified weights, or if weights is None and auto_weights=True,
        #    it will use wavelength dependent weights determined from the spectrum of the brightest objects objid on each order


    """
    def __init__(self, spec2d_files, spectrograph, det=1, offsets=None, weights='auto', sn_smooth_npix=None,
                 ir_redux=False, par=None, show=False, show_peaks=False, debug_offsets=False, debug=False, **kwargs_wave):
        super(EchelleCoadd2d, self).__init__(spec2d_files, spectrograph, det=det, offsets=offsets, weights=weights,
                                      sn_smooth_npix=sn_smooth_npix, ir_redux=ir_redux, par=par,
                                      show=show, show_peaks=show_peaks, debug_offsets=debug_offsets, debug=debug,
                                      **kwargs_wave)

        # Default wave_method for Echelle is log10
        kwargs_wave['wave_method'] = 'log10' if 'wave_method' not in kwargs_wave else kwargs_wave['wave_method']
        self.wave_grid, self.wave_grid_mid, self.dsamp = self.get_wave_grid(**kwargs_wave)

        self.objid_bri = None
        self.slitid_bri  = None
        self.snr_bar_bri = None
        if offsets is None:
            self.objid_bri, self.slitid_bri, self.snr_bar_bri = self.get_brightest_obj(self.stack_dict['specobjs_list'], self.nslits)

        self.use_weights = self.parse_weights(weights)

    def parse_weights(self, weights):

        if 'auto' in weights:
            return 'auto_echelle'
        elif 'uniform' in weights:
            return 'uniform'
        elif isinstance(weights, (list, np.ndarray)):
            if len(weights) != self.nexp:
                msgs.error('If weights are input it must be a list/array with same number of elements as exposures')
            return weights
        else:
            msgs.error('Unrecognized format for weights')

    def get_brightest_obj(self, specobjs_list, nslits):
        """
        Utility routine to find the brightest object in each exposure given a specobjs_list for echelle reductions.

        Parameters:
            specobjs_list: list
               List of SpecObjs objects.
        Optional Parameters:
            echelle: bool, default=True

        Returns:
            (objid, snr_bar)

            objid: ndarray, int, shape (len(specobjs_list),)
                Array of object ids representing the brightest object in each exposure
            snr_bar: ndarray, float, shape (len(list),)
                Average S/N over all the orders for this object

        """
        nexp = len(specobjs_list)
        nspec = specobjs_list[0][0].shape[0]

        objid = np.zeros(nexp, dtype=int)
        snr_bar = np.zeros(nexp)
        # norders = specobjs_list[0].ech_orderindx.max() + 1
        for iexp, sobjs in enumerate(specobjs_list):
            uni_objid = np.unique(sobjs.ech_objid)
            nobjs = len(uni_objid)
            order_snr = np.zeros((nslits, nobjs))
            for iord in range(nslits):
                for iobj in range(nobjs):
                    ind = (sobjs.ech_orderindx == iord) & (sobjs.ech_objid == uni_objid[iobj])
                    flux = sobjs[ind][0].optimal['COUNTS']
                    ivar = sobjs[ind][0].optimal['COUNTS_IVAR']
                    wave = sobjs[ind][0].optimal['WAVE']
                    mask = sobjs[ind][0].optimal['MASK']
                    rms_sn, weights = coadd1d.sn_weights(wave, flux, ivar, mask, self.sn_smooth_npix, const_weights=True)
                    order_snr[iord, iobj] = rms_sn

            # Compute the average SNR and find the brightest object
            snr_bar_vec = np.mean(order_snr, axis=0)
            objid[iexp] = uni_objid[snr_bar_vec.argmax()]
            snr_bar[iexp] = snr_bar_vec[snr_bar_vec.argmax()]

        self.snr_report(snr_bar)

        return objid, None, snr_bar

    def reference_trace_stack(self, slitid, offsets=None, objid=None):

        # There are two modes of operation to determine the reference trace for the Echelle 2d coadd of a given order
        # --------------------------------------------------------------------------------------------------------
        # 1) offsets: we stack about the central trace for the slit in question with the input offsets added
        # 2) ojbid: we stack about the trace of reference object for this slit given for each exposure by the input objid

        if offsets is not None and objid is not None:
            msgs.errror('You can only input offsets or an objid, but not both')
        nexp = len(offsets) if offsets is not None else len(objid)
        if offsets is not None:
            return self.offset_slit_cen(slitid, offsets)
        elif objid is not None:
            specobjs_list = self.stack_dict['specobjs_list']
            nspec = specobjs_list[0][0].trace_spat.shape[0]
            # Grab the traces, flux, wavelength and noise for this slit and objid.
            ref_trace_stack = np.zeros((nspec, nexp), dtype=float)
            for iexp, sobjs in enumerate(specobjs_list):
                ithis = (sobjs.slitid == slitid) & (sobjs.objid == objid[iexp])
                ref_trace_stack[:, iexp] = sobjs[ithis].trace_spat
            return ref_trace_stack
        else:
            msgs.error('You must input either offsets or an objid to determine the stack of reference traces')
            return None



def instantiate_me(spec2d_files, spectrograph, **kwargs):
    """
    Instantiate the CoAdd2d subclass appropriate for the provided
    spectrograph.

    The class must be subclassed from Reduce.  See :class:`Reduce` for
    the description of the valid keyword arguments.

    Args:
        spectrograph
            (:class:`pypeit.spectrographs.spectrograph.Spectrograph`):
            The instrument used to collect the data to be reduced.

        tslits_dict: dict
            dictionary containing slit/order boundary information
        tilts (np.ndarray):

    Returns:
        :class:`PypeIt`: One of the classes with :class:`PypeIt` as its
        base.
    """
    indx = [ c.__name__ == (spectrograph.pypeline + 'Coadd2d') for c in Coadd2d.__subclasses__() ]
    if not np.any(indx):
        msgs.error('Pipeline {0} is not defined!'.format(spectrograph.pypeline))
    return Coadd2d.__subclasses__()[np.where(indx)[0][0]](spec2d_files, spectrograph, **kwargs)


# Determine brightest object either if offsets were not input, or if automatic weight determiniation is desired
# if offsets is None or auto_weights is True:
#    self.objid_bri, self.slitid_bri, self.snr_bar_bri = get_brightest_obj(self.stack_dict['specobjs_list'], self.nslits)
# else:
#    self.objid_bri, self.slitid_bri, self.snr_bar_bri = None, None, None


# Echelle can either stack with:
# 1) input offsets or if offsets is None, it will find the objid of brightest trace and stack all orders relative to the trace of this object.
# 2) specified weights, or if weights is None and auto_weights=True,
#    it will use wavelength dependent weights determined from the spectrum of the brightest objects objid on each order

# if offsets is None:

# If echelle and offsets is None get the brightest object and stack about that

#
# if 'MultiSlit' in pypeline:
#     msgs.info('Determining offsets using brightest object on slit: {:d} with avg SNR={:5.2f}'.format(
#         slitid_bri, np.mean(snr_bar_bri)))
#     thismask_stack = self.stack_dict['slitmask_stack'] == slitid_bri
#     trace_stack_bri = np.zeros((nspec, nexp))
#     # TODO Need to think abbout whether we have multiple tslits_dict for each exposure or a single one
#     for iexp in range(nexp):
#         trace_stack_bri[:, iexp] = (stack_dict['tslits_dict']['slit_left'][:, slitid_bri] +
#                                     stack_dict['tslits_dict']['slit_righ'][:, slitid_bri]) / 2.0
#     # Determine the wavelength grid that we will use for the current slit/order
#     wave_bins = get_wave_bins(thismask_stack, stack_dict['waveimg_stack'], wave_grid)
#     dspat_bins, dspat_stack = get_spat_bins(thismask_stack, trace_stack_bri)
#
#     sci_list = [stack_dict['sciimg_stack'] - stack_dict['skymodel_stack'], stack_dict['waveimg_stack'], dspat_stack]
#     var_list = [utils.calc_ivar(stack_dict['sciivar_stack'])]
#
#     sci_list_rebin, var_list_rebin, norm_rebin_stack, nsmp_rebin_stack = rebin2d(
#         wave_bins, dspat_bins, stack_dict['waveimg_stack'], dspat_stack, thismask_stack,
#         (stack_dict['mask_stack'] == 0), sci_list, var_list)
#     thismask = np.ones_like(sci_list_rebin[0][0, :, :], dtype=bool)
#     nspec_psuedo, nspat_psuedo = thismask.shape
#     slit_left = np.full(nspec_psuedo, 0.0)
#     slit_righ = np.full(nspec_psuedo, nspat_psuedo)
#     inmask = norm_rebin_stack > 0
#     traces_rect = np.zeros((nspec_psuedo, nexp))
#     sobjs = specobjs.SpecObjs()
#     specobj_dict = {'setup': 'unknown', 'slitid': 999, 'orderindx': 999, 'det': det, 'objtype': 'unknown',
#                     'pypeline': pypeline + '_coadd_2d'}
#     for iexp in range(nexp):
#         sobjs_exp, _ = extract.objfind(sci_list_rebin[0][iexp, :, :], thismask, slit_left, slit_righ,
#                                        inmask=inmask[iexp, :, :], fwhm=3.0, maxdev=2.0, ncoeff=3, sig_thresh=10.0,
#                                        nperslit=1,
#                                        debug_all=debug, specobj_dict=specobj_dict)
#         sobjs.add_sobj(sobjs_exp)
#         traces_rect[:, iexp] = sobjs_exp.trace_spat
#     # Now deterimine the offsets. Arbitrarily set the zeroth trace to the reference
#     med_traces_rect = np.median(traces_rect, axis=0)
#     offsets = med_traces_rect[0] - med_traces_rect
#     if debug:
#         for iexp in range(nexp):
#             plt.plot(traces_rect[:, iexp], linestyle='--', label='original trace')
#             plt.plot(traces_rect[:, iexp] + offsets[iexp], label='shifted traces')
#             plt.legend()
#         plt.show()
#     rms_sn, weights = optimal_weights(stack_dict['specobjs_list'], slitid_bri, objid_bri,
#                                       sn_smooth_npix, const_weights=True)
#     # TODO compute the variance in the registration of the traces and write that out?
#
# coadd_list = []
# for islit in range(self.nslits):
#     msgs.info('Performing 2d coadd for slit: {:d}/{:d}'.format(islit, self.nslits - 1))
#     ref_trace_stack = reference_trace_stack(islit, self.stack_dict, offsets=offsets, objid=None)
#     # Determine the wavelength dependent optimal weights and grab the reference trace
#     if 'Echelle' in self.pypeline:
#         rms_sn, weights = optimal_weights(self.stack_dict['specobjs_list'], islit, objid_bri, sn_smooth_npix)
#
#     thismask_stack = self.stack_dict['slitmask_stack'] == islit
#     # Perform the 2d coadd
#     coadd_dict = conmpute_coadd2d(ref_trace_stack, self.stack_dict['sciimg_stack'], self.stack_dict['sciivar_stack'],
#                                   self.stack_dict['skymodel_stack'], self.stack_dict['mask_stack'] == 0,
#                                   self.stack_dict['tilts_stack'], thismask_stack, self.stack_dict['waveimg_stack'],
#                                   self.wave_grid, weights=weights)
#     coadd_list.append(coadd_dict)
#
# nspec_vec = np.zeros(self.nslits, dtype=int)
# nspat_vec = np.zeros(self.nslits, dtype=int)
# for islit, cdict in enumerate(coadd_list):
#     nspec_vec[islit] = cdict['nspec']
#     nspat_vec[islit] = cdict['nspat']
#
# # Determine the size of the psuedo image
# nspat_pad = 10
# nspec_psuedo = nspec_vec.max()
# nspat_psuedo = np.sum(nspat_vec) + (nslits + 1) * nspat_pad
# spec_vec_psuedo = np.arange(nspec_psuedo)
# shape_psuedo = (nspec_psuedo, nspat_psuedo)
# imgminsky_psuedo = np.zeros(shape_psuedo)
# sciivar_psuedo = np.zeros(shape_psuedo)
# waveimg_psuedo = np.zeros(shape_psuedo)
# tilts_psuedo = np.zeros(shape_psuedo)
# spat_img_psuedo = np.zeros(shape_psuedo)
# nused_psuedo = np.zeros(shape_psuedo, dtype=int)
# inmask_psuedo = np.zeros(shape_psuedo, dtype=bool)
# wave_mid = np.zeros((nspec_psuedo, nslits))
# wave_mask = np.zeros((nspec_psuedo, nslits), dtype=bool)
# wave_min = np.zeros((nspec_psuedo, nslits))
# wave_max = np.zeros((nspec_psuedo, nslits))
# dspat_mid = np.zeros((nspat_psuedo, nslits))
#
# spat_left = nspat_pad
# slit_left = np.zeros((nspec_psuedo, nslits))
# slit_righ = np.zeros((nspec_psuedo, nslits))
# spec_min1 = np.zeros(nslits)
# spec_max1 = np.zeros(nslits)
#
# for islit, coadd_dict in enumerate(coadd_list):
#     spat_righ = spat_left + nspat_vec[islit]
#     ispec = slice(0, nspec_vec[islit])
#     ispat = slice(spat_left, spat_righ)
#     imgminsky_psuedo[ispec, ispat] = coadd_dict['imgminsky']
#     sciivar_psuedo[ispec, ispat] = coadd_dict['sciivar']
#     waveimg_psuedo[ispec, ispat] = coadd_dict['waveimg']
#     tilts_psuedo[ispec, ispat] = coadd_dict['tilts']
#     # spat_psuedo is the sub-pixel image position on the rebinned psuedo image
#     inmask_psuedo[ispec, ispat] = coadd_dict['outmask']
#     image_temp = (coadd_dict['dspat'] - coadd_dict['dspat_mid'][0] + spat_left) * coadd_dict['outmask']
#     spat_img_psuedo[ispec, ispat] = image_temp
#     nused_psuedo[ispec, ispat] = coadd_dict['nused']
#     wave_min[ispec, islit] = coadd_dict['wave_min']
#     wave_max[ispec, islit] = coadd_dict['wave_max']
#     wave_mid[ispec, islit] = coadd_dict['wave_mid']
#     wave_mask[ispec, islit] = True
#     # Fill in the rest of the wave_mid with the corresponding points in the wave_grid
#     wave_this = wave_mid[wave_mask[:, islit], islit]
#     ind_upper = np.argmin(np.abs(wave_grid_mid - np.max(wave_this.max()))) + 1
#     if nspec_vec[islit] != nspec_psuedo:
#         wave_mid[nspec_vec[islit]:, islit] = wave_grid_mid[ind_upper:ind_upper + (nspec_psuedo - nspec_vec[islit])]
#
#     dspat_mid[ispat, islit] = coadd_dict['dspat_mid']
#     slit_left[:, islit] = np.full(nspec_psuedo, spat_left)
#     slit_righ[:, islit] = np.full(nspec_psuedo, spat_righ)
#     spec_max1[islit] = nspec_vec[islit] - 1
#     spat_left = spat_righ + nspat_pad
#
# slitcen = (slit_left + slit_righ) / 2.0
# tslits_dict_psuedo = dict(slit_left=slit_left, slit_righ=slit_righ, slitcen=slitcen,
#                           nspec=nspec_psuedo, nspat=nspat_psuedo, pad=0,
#                           nslits=self.nslits, binspectral=1, binspatial=1, spectrograph=spectrograph.spectrograph,
#                           spec_min=spec_min1, spec_max=spec_max1,
#                           maskslits=np.zeros(slit_left.shape[1], dtype=np.bool))
#
# slitmask_psuedo = pixels.tslits2mask(tslits_dict_psuedo)
# # This is a kludge to deal with cases where bad wavelengths result in large regions where the slit is poorly sampled,
# # which wreaks havoc on the local sky-subtraction
# min_slit_frac = 0.70
# spec_min = np.zeros(self.nslits)
# spec_max = np.zeros(self.nslits)
# for islit in range(self.nslits):
#     slit_width = np.sum(inmask_psuedo * (slitmask_psuedo == islit), axis=1)
#     slit_width_img = np.outer(slit_width, np.ones(nspat_psuedo))
#     med_slit_width = np.median(slit_width_img[slitmask_psuedo == islit])
#     nspec_eff = np.sum(slit_width > min_slit_frac * med_slit_width)
#     nsmooth = int(np.fmax(np.ceil(nspec_eff * 0.02), 10))
#     slit_width_sm = scipy.ndimage.filters.median_filter(slit_width, size=nsmooth, mode='reflect')
#     igood = (slit_width_sm > min_slit_frac * med_slit_width)
#     spec_min[islit] = spec_vec_psuedo[igood].min()
#     spec_max[islit] = spec_vec_psuedo[igood].max()
#     bad_pix = (slit_width_img < min_slit_frac * med_slit_width) & (slitmask_psuedo == islit)
#     inmask_psuedo[bad_pix] = False
#
# # Update with tslits_dict_psuedo
# tslits_dict_psuedo['spec_min'] = spec_min
# tslits_dict_psuedo['spec_max'] = spec_max
# slitmask_psuedo = pixels.tslits2mask(tslits_dict_psuedo)
#
# # Make a fake bitmask from the outmask. We are kludging the crmask to be the outmask_psuedo here, and setting the bpm to
# # be good everywhere
# # mask = processimages.ProcessImages.build_mask(imgminsky_psuedo, sciivar_psuedo, np.invert(inmask_psuedo),
# #                                              np.zeros_like(inmask_psuedo), slitmask=slitmask_psuedo)
#
# # Generate a ScienceImage
# sciImage = scienceimage.ScienceImage.from_images(self.spectrograph, det,
#                                                  self.par['scienceframe']['process'],
#                                                  np.zeros_like(inmask_psuedo),  # Dummy bpm
#                                                  imgminsky_psuedo, sciivar_psuedo,
#                                                  np.zeros_like(inmask_psuedo),  # Dummy rn2img
#                                                  crmask=np.invert(inmask_psuedo))
# sciImage.build_mask(slitmask=slitmask_psuedo)
#
# redux = reduce.instantiate_me(sciImage, self.spectrograph, tslits_dict_psuedo, par, tilts_psuedo, ir_redux=ir_redux,
#                               objtype='science', binning=self.binning)
#
# if show:
#     redux.show('image', image=imgminsky_psuedo * (sciImage.mask == 0), chname='imgminsky', slits=True, clear=True)
# # Object finding
# sobjs_obj, nobj, skymask_init = redux.find_objects(sciImage.image, ir_redux=ir_redux, show_peaks=show_peaks, show=show)
# # Local sky-subtraction
# global_sky_psuedo = np.zeros_like(imgminsky_psuedo)  # No global sky for co-adds since we go straight to local
# skymodel_psuedo, objmodel_psuedo, ivarmodel_psuedo, outmask_psuedo, sobjs = \
#     redux.local_skysub_extract(waveimg_psuedo, global_sky_psuedo, sobjs_obj, spat_pix=spat_psuedo,
#                                model_noise=False, show_profile=show, show=show)
#
# if ir_redux:
#     sobjs.purge_neg()
#
# # Add the information about the fixed wavelength grid to the sobjs
# for spec in sobjs:
#     spec.boxcar['WAVE_GRID_MASK'] = wave_mask[:, spec.slitid]
#     spec.boxcar['WAVE_GRID'] = wave_mid[:, spec.slitid]
#     spec.boxcar['WAVE_GRID_MIN'] = wave_min[:, spec.slitid]
#     spec.boxcar['WAVE_GRID_MAX'] = wave_max[:, spec.slitid]
#
#     spec.optimal['WAVE_GRID_MASK'] = wave_mask[:, spec.slitid]
#     spec.optimal['WAVE_GRID'] = wave_mid[:, spec.slitid]
#     spec.optimal['WAVE_GRID_MIN'] = wave_min[:, spec.slitid]
#     spec.optimal['WAVE_GRID_MAX'] = wave_max[:, spec.slitid]
#
# # TODO Implement flexure and heliocentric corrections on the single exposure 1d reductions and apply them to the
# # waveimage. Change the data model to accomodate a wavelength model for each image.
# # Using the same implementation as in core/pypeit
#
# # Write out the psuedo master files to disk
# master_key_dict = self.stack_dict['master_key_dict']
#
# # TODO: These saving operations are a temporary kludge
# waveImage = WaveImage(None, None, None, None, None, None, master_key=master_key_dict['arc'],
#                       master_dir=master_dir)
# waveImage.save(image=waveimg_psuedo)
#
# traceSlits = TraceSlits(None, None, master_key=master_key_dict['trace'], master_dir=master_dir)
# traceSlits.save(tslits_dict=tslits_dict_psuedo)

# return imgminsky_psuedo, sciivar_psuedo, skymodel_psuedo, objmodel_psuedo, ivarmodel_psuedo, outmask_psuedo, sobjs
#
#
#
# def get_brightest_obj(specobjs_list, nslits, pypeline):
#     """
#     Utility routine to find the brightest object in each exposure given a specobjs_list. This currently only works
#     for echelle.
#
#     Parameters:
#         specobjs_list: list
#            List of SpecObjs objects.
#     Optional Parameters:
#         echelle: bool, default=True
#
#     Returns:
#         (objid, slitid, snr_bar), tuple
#
#         objid: ndarray, int, shape (len(specobjs_list),)
#             Array of object ids representing the brightest object in each exposure
#         slitid (int):
#             Slit that highest S/N ratio object is on (only for pypeline=MultiSlit)
#         snr_bar: ndarray, float, shape (len(list),)
#             Average S/N over all the orders for this object
#
#     """
#     nexp = len(specobjs_list)
#     nspec = specobjs_list[0][0].shape[0]
#     if 'Echelle' in pypeline:
#         objid = np.zeros(nexp, dtype=int)
#         snr_bar = np.zeros(nexp)
#         #norders = specobjs_list[0].ech_orderindx.max() + 1
#         for iexp, sobjs in enumerate(specobjs_list):
#             uni_objid = np.unique(sobjs.ech_objid)
#             nobjs = len(uni_objid)
#             order_snr = np.zeros((nslits, nobjs))
#             for iord in range(nslits):
#                 for iobj in range(nobjs):
#                     ind = (sobjs.ech_orderindx == iord) & (sobjs.ech_objid == uni_objid[iobj])
#                     flux = sobjs[ind][0].optimal['COUNTS']
#                     ivar = sobjs[ind][0].optimal['COUNTS_IVAR']
#                     wave = sobjs[ind][0].optimal['WAVE']
#                     mask = sobjs[ind][0].optimal['MASK']
#                     rms_sn, weights = coadd1d.sn_weights(wave, flux, ivar, mask, const_weights=True)
#                     order_snr[iord, iobj] = rms_sn
#
#             # Compute the average SNR and find the brightest object
#             snr_bar_vec = np.mean(order_snr, axis=0)
#             objid[iexp] = uni_objid[snr_bar_vec.argmax()]
#             snr_bar[iexp] = snr_bar_vec[snr_bar_vec.argmax()]
#             slitid = None
#     else:
#         slit_snr_max = np.full((nslits, nexp), -np.inf)
#         objid_max = np.zeros((nslits, nexp),dtype=int)
#         # Loop over each exposure, slit, find the brighest object on that slit for every exposure
#         for iexp, sobjs in enumerate(specobjs_list):
#             for islit in range(nslits):
#                 ithis = sobjs.slitid == islit
#                 nobj_slit = np.sum(ithis)
#                 if np.any(ithis):
#                     objid_this = sobjs[ithis].objid
#                     flux = np.zeros((nspec, nobj_slit))
#                     ivar = np.zeros((nspec, nobj_slit))
#                     wave = np.zeros((nspec, nobj_slit))
#                     mask = np.zeros((nspec, nobj_slit), dtype=bool)
#                     for iobj, spec in enumerate(sobjs[ithis]):
#                         flux[:, iobj] = spec.optimal['COUNTS']
#                         ivar[:,iobj]  = spec.optimal['COUNTS_IVAR']
#                         wave[:,iobj]  = spec.optimal['WAVE']
#                         mask[:,iobj]  = spec.optimal['MASK']
#                     rms_sn, weights = coadd1d.sn_weights(wave, flux, ivar, mask, None, const_weights=True)
#                     imax = np.argmax(rms_sn)
#                     slit_snr_max[islit, iexp] = rms_sn[imax]
#                     objid_max[islit, iexp] = objid_this[imax]
#         # Find the highest snr object among all the slits
#         slit_snr = np.mean(slit_snr_max, axis=1)
#         slitid = slit_snr.argmax()
#         snr_bar_mean = slit_snr[slitid]
#         snr_bar = slit_snr_max[slitid, :]
#         objid = objid_max[slitid, :]
#         if (snr_bar_mean == -np.inf):
#             msgs.error('You do not appear to have a unique reference object that was traced as the highest S/N '
#                        'ratio on the same slit of every exposure')
#
#     # Print out a report on the SNR
#     msg_string =          msgs.newline() + '-------------------------------------'
#     msg_string +=         msgs.newline() + '  Summary for highest S/N object'
#     if 'MultiSlit' in pypeline:
#         msg_string +=     msgs.newline() + '      found on slitid = {:d}            '.format(slitid)
#
#     msg_string +=         msgs.newline() + '-------------------------------------'
#     msg_string +=         msgs.newline() + '           exp#       S/N'
#     for iexp, snr in enumerate(snr_bar):
#         msg_string +=     msgs.newline() + '            {:d}        {:5.2f}'.format(iexp, snr)
#
#     msg_string +=         msgs.newline() + '-------------------------------------'
#     msgs.info(msg_string)
#
#     return objid, slitid, snr_bar
